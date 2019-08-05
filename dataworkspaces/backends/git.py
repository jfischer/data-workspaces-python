"""
Git backend for storing a workspace
"""

import os
from os.path import exists, join, isdir, basename, isabs, abspath, expanduser, dirname, curdir
import shutil
import json
import re
import uuid
from typing import Any, Iterable, Optional, List, Dict
assert Dict # make pyflakes happy

import click

import dataworkspaces.workspace as ws
from dataworkspaces.workspace import JSONDict, SnapshotMetadata
from dataworkspaces.errors import ConfigurationError, InternalError
from dataworkspaces.utils.subprocess_utils import call_subprocess
from dataworkspaces.utils.git_utils import \
    commit_changes_in_repo, git_init, git_add,\
    validate_git_fat_in_path_if_needed, \
    run_git_fat_pull_if_needed, is_git_dirty,\
    is_pull_needed_from_remote, GIT_EXE_PATH,\
    run_git_fat_push_if_needed,\
    set_remote_origin, is_a_git_fat_repo,\
    validate_git_fat_in_path
from dataworkspaces.utils.file_utils import safe_rename


BASE_DIR='.dataworkspace'
GIT_IGNORE_FILE_PATH='.dataworkspace/.gitignore'
CONFIG_FILE_PATH='.dataworkspace/config.json'
LOCAL_PARAMS_PATH='.dataworkspace/local_params.json'
RESOURCES_FILE_PATH='.dataworkspace/resources.json'
RESOURCE_LOCAL_PARAMS_PATH='.dataworkspace/resource_local_params.json'
SNAPSHOT_DIR_PATH='.dataworkspace/snapshots'
SNAPSHOT_METADATA_DIR_PATH='.dataworkspace/snapshot_metadata'


class Workspace(ws.Workspace, ws.SyncedWorkspaceMixin, ws.SnapshotWorkspaceMixin):
    def __init__(self, workspace_dir:str, batch:bool=False,
                 verbose:bool=False):
        self.workspace_dir = workspace_dir
        cf_data = self._load_json_file(CONFIG_FILE_PATH)
        super().__init__(cf_data['name'], cf_data['dws-version'], batch, verbose)
        self.global_params = cf_data['global_params']
        self.local_params = self._load_json_file(LOCAL_PARAMS_PATH)
        self.resource_params = self._load_json_file(RESOURCES_FILE_PATH) # type: List[JSONDict]
        self.resource_params_by_name = {} # type: Dict[str, JSONDict]
        for r in self.resource_params:
            self.resource_params_by_name[r['name']] = r
        self.resource_local_params_by_name = \
            self._load_json_file(RESOURCE_LOCAL_PARAMS_PATH) # type: Dict[str,JSONDict]

    def _load_json_file(self, relative_path):
        f_path = join(self.workspace_dir, relative_path)
        if not exists(f_path):
            raise ConfigurationError("Did not find workspace metadata file %s"
                                     % f_path)
        with open(f_path, 'r') as f:
            return json.load(f)

    def _save_json_to_file(self, obj, relative_path):
        f_path = join(self.workspace_dir, relative_path)
        with open(f_path, 'w') as f:
            json.dump(obj, f, indent=2)

    def _get_global_params(self) -> JSONDict:
        """Get a dict of configuration parameters for this workspace,
        which apply across all instances.
        """
        return self.global_params

    def _get_local_params(self) -> JSONDict:
        """Get a dict of configuration parameters for this particular
        install of the workspace (e.g. local filesystem paths, hostname).
        """
        return self.local_params

    def _set_global_param(self, name:str, value:Any) -> None:
        """Setting does not necessarily take effect until save() is called"""
        data = self._get_global_params()
        data[name] = value
        self._save_json_to_file({'name':self.name,
                                 'dws-version':self.dws_version,
                                 'global_params':data},
                                CONFIG_FILE_PATH)

    def _set_local_param(self, name:str, value:Any) -> None:
        data = self._get_local_params()
        data[name] = value
        self._save_json_to_file(data, LOCAL_PARAMS_PATH)

    def get_resource_names(self) -> Iterable[str]:
        return self.resource_params_by_name.keys()

    def _get_resource_params(self, resource_name) -> JSONDict:
        """Get the parameters for this resource from the workspace's
        metadata store - used when instantitating resources. Show
        throw an exception if resource does not exist.
        """
        if resource_name not in self.resource_params_by_name:
            raise ConfigurationError("A resource by the name '%s' does not exist in this workspace"%
                                     resource_name)
        return self.resource_params_by_name[resource_name]

    def _get_resource_local_params(self, resource_name:str) -> Optional[JSONDict]:
        """If a resource has local parameters defined for it, return them.
        Otherwise, return None.
        """
        if resource_name in self.resource_local_params_by_name:
            return self.resource_local_params_by_name[resource_name]
        else:
            return None

    def _add_params_for_resource(self, resource_name:str, params:JSONDict) -> None:
        """
        Add the necessary state for a new resource to the workspace.
        """
        assert params['name']==resource_name
        self.resource_params.append(params)
        self.resource_params_by_name[resource_name] = params
        self._save_json_to_file(self.resource_params, RESOURCES_FILE_PATH)

    def _add_local_params_for_resource(self, resource_name:str,
                                       local_params:JSONDict)->None:
        """
        Add local params either for a new or cloned resource.
        """
        self.resource_local_params_by_name[resource_name] = local_params
        self._save_json_to_file(self.resource_local_params_by_name,
                                RESOURCE_LOCAL_PARAMS_PATH)

    def get_workspace_local_path_if_any(self) -> Optional[str]:
        return self.workspace_dir

    def _add_local_dir_to_gitignore_if_needed(self, resource):
        """Figure out whether resource has a local path under the workspace's
        git repo, which needs to be added to .gitignore. If so, do it.
        """
        if resource.resource_type=='git-subdirectory':
            return  # this is always a part of the dataworkspace's repo
        elif  not isinstance(resource, ws.LocalStateResourceMixin):
            return # no local state, so not an iddue
        local_path = resource.get_local_path_if_any()
        if local_path is None:
            return
        assert isabs(local_path), "Resource local path should be absolute"
        if not local_path.startswith(self.workspace_dir):
            return None
        local_relpath = local_path[len(self.workspace_dir)+1:]
        if not local_relpath.endswith('/'):
            local_relpath_noslash = local_relpath
            local_relpath = local_relpath + '/'
        else:
            local_relpath_noslash = local_relpath[:-1]
        # Add a / as the start to indicate that the path starts at the root of the repo.
        # Otherwise, we'll hit cases where the path could match other directories (e.g. issue #11)
        local_relpath = '/'+local_relpath if not local_relpath.startswith('/') else local_relpath
        local_relpath_noslash = '/'+local_relpath_noslash \
                                if not local_relpath_noslash.startswith('/') \
                                else local_relpath_noslash
        gitignore_path = join(self.workspace_dir, '.gitignore')
        # read the gitignore file to see if relpath is already there
        if exists(gitignore_path):
            with open(gitignore_path, 'r') as f:
                for line in f:
                    line = line.rstrip()
                    if line==local_relpath or line==local_relpath_noslash:
                        return # no need to add
        with open(gitignore_path, 'a') as f:
            f.write(local_relpath+ '\n')

    def add_resource(self, name:str, resource_type:str, role:str, *args, **kwargs)\
        -> ws.Resource:
        r = super().add_resource(name, resource_type, role, *args, **kwargs)
        self._add_local_dir_to_gitignore_if_needed(r)
        return r

    def clone_resource(self, name:str) -> ws.LocalStateResourceMixin:
        """Only called if the resource has local state....
        """
        r = super().clone_resource(name)
        self._add_local_dir_to_gitignore_if_needed(r)
        return r
    
    def save(self, message:str) -> None:
        """Save the current state of the workspace"""
        commit_changes_in_repo(self.workspace_dir, message, verbose=self.verbose)

    def pull_workspace(self) -> ws.SyncedWorkspaceMixin:
        # first, check for problems
        if is_git_dirty(self.workspace_dir):
            raise ConfigurationError("Data workspace metadata repo at %s has uncommitted changes. Please commit before pulling." %
                                     self.workspace_dir)
        validate_git_fat_in_path_if_needed(self.workspace_dir)

        # do the pooling
        call_subprocess([GIT_EXE_PATH, 'pull', 'origin', 'master'],
                        cwd=self.workspace_dir, verbose=self.verbose)
        run_git_fat_pull_if_needed(self.workspace_dir, self.verbose)

        # reload and return new workspace
        return Workspace(self.workspace_dir, batch=self.batch,
                         verbose=self.verbose)

    def _push_precheck(self, resource_list:List[ws.LocalStateResourceMixin]) -> None:
        if is_git_dirty(self.workspace_dir):
            raise ConfigurationError("Data workspace metadata repo at %s has uncommitted changes. Please commit before pushing." %
                                     self.workspace_dir)
        if is_pull_needed_from_remote(self.workspace_dir, 'master', self.verbose):
            raise ConfigurationError("Data workspace at %s requires a pull from remote origin"%
                                     self.workspace_dir)
        validate_git_fat_in_path_if_needed(self.workspace_dir)
        super()._push_precheck(resource_list)

    def push(self, resource_list:List[ws.LocalStateResourceMixin]) -> None:
        super().push(resource_list)
        call_subprocess([GIT_EXE_PATH, 'push', 'origin', 'master'],
                        cwd=self.workspace_dir, verbose=self.verbose)
        run_git_fat_push_if_needed(self.workspace_dir, verbose=self.verbose)

    def publish(self, *args) -> None:
        if len(args)!=1:
            raise InternalError("publish takes one argument: remote_repository, got %s"%
                                args)
        set_remote_origin(self.workspace_dir, args[0],
                          verbose=self.verbose)

    def get_next_snapshot_number(self) -> int:
        """Snapshot numbers are assigned based on how many snapshots have
        already been taken. Counting starts at 1. Note that snaphsot
        numbers are not necessarily unique, as people could simultaneously
        take snapshots in different copies of the workspace. Thus, we
        usually combine the snapshot with the hostname.
        """
        md_dirpath = join(self.workspace_dir, SNAPSHOT_METADATA_DIR_PATH)
        if not isdir(md_dirpath):
            return 1 # first snapshot
        # we recursively walk the tree to be future-proof in case we
        # find that we need to start putting metadata into subdirectories.
        def process_dir(dirpath):
            cnt=0
            for f in os.listdir(dirpath):
                p = join(dirpath, f)
                if isdir(p):
                    cnt += process_dir(p)
                elif f.endswith('_md.json'):
                    cnt += 1
            return cnt
        return 1 + process_dir(md_dirpath)

    def get_snapshot_metadata(self, hash_val:str) -> SnapshotMetadata:
        hash_val = hash_val.lower()
        md_filename = join(join(self.workspace_dir, SNAPSHOT_METADATA_DIR_PATH),
                           '%s_md.json'%hash_val)
        if not exists(md_filename):
            raise ConfigurationError("No metadata entry for snapshot %s"%hash_val)
        with open(md_filename, 'r') as f:
            data = json.load(f)
        md = ws.SnapshotMetadata.from_json(data)
        assert md.hashval==hash_val
        return md


    def get_snapshot_by_tag(self, tag:str) -> SnapshotMetadata:
        """Given a tag, return the asssociated snapshot metadata.
        This lookup could be slower ,if a reverse index is not kept."""
        md_dir = join(self.workspace_dir, SNAPSHOT_METADATA_DIR_PATH)
        regexp = re.compile(re.escape(tag))
        for fname in os.listdir(md_dir):
            if not fname.endswith('_md.json'):
                continue
            fpath = join(md_dir, fname)
            with open(fpath, 'r') as f:
                raw_data = f.read()
            if regexp.search(raw_data) is not None:
                md = SnapshotMetadata.from_json(json.loads(raw_data))
                if md.has_tag(tag):
                    return md
        raise ConfigurationError("Snapshot for tag %s not found" % tag)

    def get_snapshot_by_partial_hash(self, partial_hash:str) -> SnapshotMetadata:
        """Given a partial hash for the snapshot, find the snapshot whose hash
        starts with this prefix and return the metadata
        asssociated with the snapshot.
        """
        partial_hash = partial_hash.lower()
        md_dir = join(self.workspace_dir, SNAPSHOT_METADATA_DIR_PATH)
        for fname in os.listdir(md_dir):
            if not fname.endswith('_md.json'):
                continue
            hashval = fname[0:-8].lower()
            if not hashval.startswith(partial_hash):
                continue
            return self.get_snapshot_metadata(hashval)
        raise ConfigurationError("Snapshot match for partial hash %s not found" %
                                 partial_hash)

    def _get_snapshot_manifest_as_bytes(self, hash_val:str) -> bytes:
        snapshot_dir = join(self.workspace_dir, SNAPSHOT_DIR_PATH)
        snapshot_file = join(snapshot_dir, 'snapshot-%s.json'%hash_val.lower())
        if not exists(snapshot_file):
            raise ConfigurationError("No snapshot found for hash value %s" % hash_val)
        with open(snapshot_file, 'rb') as f:
            return f.read()

    def list_snapshots(self, reverse:bool=True, max_count:Optional[int]=None)\
        -> Iterable[SnapshotMetadata]:
        """Returns an iterable of snapshot metadata, sorted by timestamp ascending
        (or descending if reverse is True). If max_count is specified, return at
        most that many snaphsots.
        """
        md_dir = join(self.workspace_dir, SNAPSHOT_METADATA_DIR_PATH)
        snapshots = []
        for fname in os.listdir(md_dir):
            if not fname.endswith('_md.json'):
                continue
            with open(join(md_dir, fname), 'r') as f:
                snapshots.append(SnapshotMetadata.from_json(json.load(f)))
        snapshots.sort(key=lambda md:md.timestamp, reverse=reverse)
        return snapshots if max_count is None else snapshots[0:max_count]

    def _delete_snapshot_metadata_and_manifest(self, hash_val:str)-> None:
        """Given a snapshot hash, delete the associated metadata.
        """
        raise NotImplementedError("delete snapshot")

    def _snapshot_precheck(self, current_resources:Iterable[ws.Resource]) -> None:
        """Run any prechecks before taking a snapshot. This should throw
        a ConfigurationError if the snapshot would fail for some reason.
        """
        # call prechecks on the individual resources
        super()._snapshot_precheck(current_resources)
        validate_git_fat_in_path_if_needed(self.workspace_dir)

    def _restore_precheck(self, restore_hashes:Dict[str,str],
                          restore_resources:List[ws.SnapshotResourceMixin]) -> None:
        """Run any prechecks before restoring. This should throw
        a ConfigurationError if the restore would fail for some reason.
        """
        # call prechecks on the individual resources
        super()._restore_precheck(restore_hashes, restore_resources)
        validate_git_fat_in_path_if_needed(self.workspace_dir)

    def restore(self, restore_hashes:Dict[str,str],
                restore_resources:List[ws.SnapshotResourceMixin]) -> None:
        """We override restore to perform a git-fat pull at the end,
        if needed.
        """
        super().restore(restore_hashes, restore_resources)
        run_git_fat_pull_if_needed(self.workspace_dir, self.verbose)

    def remove_tag_from_snapshot(self, hash_val:str, tag:str) -> None:
        """Remove the specified tag from the specified snapshot. Throw an
        InternalError if either the snapshot or the tag do not exist.
        """
        md_filename = join(join(self.workspace_dir, SNAPSHOT_METADATA_DIR_PATH),
                           '%s_md.json'%hash_val.lower())
        if not exists(md_filename):
            raise InternalError("No metadata entry for snapshot %s"%hash_val)
        with open(md_filename, 'r') as f:
            data = json.load(f)
        md = ws.SnapshotMetadata.from_json(data)
        assert md.hashval==hash_val
        if tag not in md.tags:
            raise InternalError("Tag %s not found in snapshot %s" % (tag, hash_val))
        md.tags = [tag for tag in md.tags if tag!=tag]
        with open(md_filename, 'w') as f:
            json.dump(md.to_json(), f, indent=2)

    def save_snapshot_metadata_and_manifest(self, metadata:SnapshotMetadata,
                                            manifest:bytes) -> None:
        snapshot_dir_path = join(self.workspace_dir, SNAPSHOT_DIR_PATH)
        if not exists(snapshot_dir_path):
            os.makedirs(snapshot_dir_path)
        snapshot_manifest_path = join(snapshot_dir_path,
                                      'snapshot-%s.json'%metadata.hashval)
        with open(snapshot_manifest_path, 'wb') as f:
            f.write(manifest)
        snapshot_md_dir = join(self.workspace_dir, SNAPSHOT_METADATA_DIR_PATH)
        if not exists(snapshot_md_dir):
            os.makedirs(snapshot_md_dir)
        snapshot_metadata_path = join(snapshot_md_dir,
                                      '%s_md.json'%metadata.hashval)
        with open(snapshot_metadata_path, 'w') as mdf:
            json.dump(metadata.to_json(), mdf, indent=2)



class WorkspaceFactory(ws.WorkspaceFactory):
    @staticmethod
    def load_workspace(batch:bool, verbose:bool, workspace_dir:str) -> ws.Workspace: # type: ignore
        return Workspace(workspace_dir, batch, verbose)

    @staticmethod
    def init_workspace(workspace_name:str, dws_version:str, # type: ignore
                       global_params:JSONDict, local_params:JSONDict,
                       batch:bool, verbose:bool,
                       workspace_dir:str) -> ws.Workspace:
        if not exists(workspace_dir):
            raise ConfigurationError("Directory for new workspace '%s' does not exist"%
                                     workspace_dir)
        md_dir = join(workspace_dir, BASE_DIR)
        if isdir(md_dir):
            raise ConfigurationError("Found %s directory under %s"
                                     %(BASE_DIR, workspace_dir) +
                                     " Has this workspace already been initialized?")
        os.mkdir(md_dir)
        snapshot_dir = join(workspace_dir, SNAPSHOT_DIR_PATH)
        os.mkdir(snapshot_dir)
        snapshot_md_dir = join(workspace_dir, SNAPSHOT_METADATA_DIR_PATH)
        os.mkdir(snapshot_md_dir)
        with open(join(workspace_dir, CONFIG_FILE_PATH), 'w') as f:
            json.dump({'name':workspace_name, 'dws-version':dws_version,
                       'global_params':global_params},
                      f, indent=2)
        with open(join(workspace_dir, RESOURCES_FILE_PATH), 'w') as f:
            json.dump([], f, indent=2)
        with open(join(workspace_dir, LOCAL_PARAMS_PATH), 'w') as f:
            json.dump(local_params, f, indent=2)
        with open(join(workspace_dir, RESOURCE_LOCAL_PARAMS_PATH), 'w') as f:
            json.dump({}, f, indent=2)

        with open(join(workspace_dir, GIT_IGNORE_FILE_PATH), 'w') as f:
                f.write("%s\n" % basename(LOCAL_PARAMS_PATH))
                f.write("%s\n" % basename(RESOURCE_LOCAL_PARAMS_PATH))
                f.write("current_lineage/\n")
        if exists(join(workspace_dir, '.git')):
            click.echo("%s is already a git repository, will just add to it"%
                       workspace_dir)
        else:
            git_init(workspace_dir, verbose=verbose)
        git_add(workspace_dir,
                [CONFIG_FILE_PATH, RESOURCES_FILE_PATH, GIT_IGNORE_FILE_PATH],
                verbose=verbose)
        commit_changes_in_repo(workspace_dir, "dws init", verbose=verbose)
        return Workspace(workspace_dir, batch, verbose)

    @staticmethod
    def clone_workspace(local_params:JSONDict, batch:bool, verbose:bool, *args) -> ws.Workspace:
        # args is REPOSITORY_URL [DIRECTORY]
        if len(args)==0:
            raise ConfigurationError("Need to specify a Git repository URL when cloning a workspace")
        else:
            repository = args[0] # type: str
        directory = args[1] if len(args)==2 else None # type: Optional[str]
        if len(args)>2:
            raise ConfigurationError("Clone of git backend expecting at most two arguments, received: %s"%
                                     repr(args))

        # initial checks on the directory
        if directory:
            directory = abspath(expanduser(directory))
            parent_dir = dirname(directory)
            if isdir(directory):
                raise ConfigurationError("Clone target directory '%s' already exists"% directory)
            initial_path = directory
        else:
            parent_dir = abspath(expanduser(curdir))
            initial_path = join(parent_dir, uuid.uuid4().hex) # get a unique name within this directory
        if not isdir(parent_dir):
            raise ConfigurationError("Parent directory '%s' does not exist" % parent_dir)
        if not os.access(parent_dir, os.W_OK):
            raise ConfigurationError("Unable to write into directory '%s'" % parent_dir)

        # ping the remote repo
        cmd = [GIT_EXE_PATH, 'ls-remote', '--quiet', repository]
        try:
            call_subprocess(cmd, parent_dir, verbose)
        except Exception as e:
            raise ConfigurationError("Unable to access remote repository '%s'" % repository) from e

        # we have to clone the repo first to find out its name!
        try:
            cmd = [GIT_EXE_PATH, 'clone', repository, initial_path]
            call_subprocess(cmd, parent_dir, verbose)
            config_file = join(initial_path, CONFIG_FILE_PATH)
            if not exists(config_file):
                raise ConfigurationError("Did not find configuration file in remote repo")
            with open(config_file, 'r') as f:
                config_json = json.load(f)
            if 'name' not in config_json:
                raise InternalError("Missing 'name' property in configuration file")
            workspace_name = config_json['name']
            if directory is None:
                new_name = join(parent_dir, workspace_name)
                if isdir(new_name):
                    raise ConfigurationError("Clone target directory %s already exists" % new_name)
                safe_rename(initial_path, new_name)
                directory = new_name
            with open(join(directory, LOCAL_PARAMS_PATH), 'w') as f:
                json.dump(local_params, f, indent=2) # create an initial local params file
            with open(join(directory, RESOURCE_LOCAL_PARAMS_PATH), 'w') as f:
                json.dump({}, f, indent=2) # create resource local params, to be populated via resource clones
            snapshot_md_dir = join(directory, SNAPSHOT_METADATA_DIR_PATH)
            if not exists(snapshot_md_dir):
                # It is possible that we are cloning a repo with no snapshots
                os.mkdir(snapshot_md_dir)
            snapshot_dir = join(directory, SNAPSHOT_DIR_PATH)
            if not exists(snapshot_dir):
                # It is possible that we are cloning a repo with no snapshots
                os.mkdir(snapshot_dir)
            if is_a_git_fat_repo(directory):
                validate_git_fat_in_path()
                import dataworkspaces.third_party.git_fat as git_fat
                python2_exe = git_fat.find_python2_exe()
                git_fat.run_git_fat(python2_exe, ['init'], cwd=directory,
                                    verbose=verbose)
                # pull the objects referenced by the current head
                git_fat.run_git_fat(python2_exe, ['pull'], cwd=directory,
                                    verbose=verbose)
        except:
            if isdir(initial_path):
                shutil.rmtree(initial_path)
            if (directory is not None) and isdir(directory):
                shutil.rmtree(directory)
            raise

        return WorkspaceFactory.load_workspace(batch, verbose, directory)

FACTORY=WorkspaceFactory()