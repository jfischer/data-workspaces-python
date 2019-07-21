# Copyright 2018,2019 by MPI-SWS and Data-ken Research. Licensed under Apache 2.0. See LICENSE.txt.
"""
Resource for git repositories
"""
import subprocess
import os
from os.path import realpath, basename, isdir, join, dirname, exists,\
                    abspath, expanduser
import stat
import click
import shutil
from typing import Set, Pattern

from dataworkspaces.errors import ConfigurationError, InternalError
from dataworkspaces.utils.subprocess_utils import \
    call_subprocess, call_subprocess_for_rc
from dataworkspaces.utils.git_utils import \
    is_git_dirty, is_file_tracked_by_git,\
    get_local_head_hash, get_remote_head_hash,\
    commit_changes_in_repo, checkout_and_apply_commit, GIT_EXE_PATH,\
    is_git_repo, commit_changes_in_repo_subdir,\
    checkout_subdir_and_apply_commit, is_a_git_fat_repo,\
    has_git_fat_been_initialized, validate_git_fat_in_path,\
    validate_git_fat_in_path_if_needed, get_subdirectory_hash
from dataworkspaces.workspace import Resource, ResourceFactory, ResourceRoles,\
    RESOURCE_ROLE_PURPOSES, LocalStateResourceMixin, FileResourceMixin
import dataworkspaces.backends.git as git_backend
from .resource import LocalPathType
from dataworkspaces.utils.snapshot_utils import move_current_files_local_fs



def is_pull_needed_from_remote(cwd, branch, verbose):
    """Do check whether we need a pull, we get the hash of the HEAD
    of the remote's master branch. Then, we see if we have this object locally.
    """
    hashval = get_remote_head_hash(cwd, branch, verbose)
    if hashval is None:
        return False
    #cmd = [GIT_EXE_PATH, 'show', '--oneline', hashval]
    cmd = [GIT_EXE_PATH, 'cat-file', '-e', hashval+'^{commit}']
    rc = call_subprocess_for_rc(cmd, cwd, verbose=verbose)
    return rc!=0


def git_move_and_add(srcabspath, destabspath, git_root, verbose):
    """
    Move a file that might or might not be tracked by git to
    a new location (snapshot directory), set it to read-only and make sure
    that it is now tracked by git.
    """
    assert srcabspath.startswith(git_root)
    assert destabspath.startswith(git_root)
    srcrelpath = srcabspath[len(git_root)+1:]
    destrelpath = destabspath[len(git_root)+1:]
    if is_file_tracked_by_git(srcrelpath, git_root, verbose):
        call_subprocess([GIT_EXE_PATH, 'mv',
                         srcrelpath, destrelpath],
                        cwd=git_root,
                        verbose=verbose)
    else:
        # file is not tracked by git yet, just move and then add to git
        os.rename(join(git_root, srcrelpath),
                  join(git_root, destrelpath))
    # either way, we change the permissions and then do an add at the end
    mode = os.stat(destabspath)[stat.ST_MODE]
    os.chmod(destabspath, mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
    call_subprocess([GIT_EXE_PATH, 'add', destrelpath],
                    cwd=git_root, verbose=verbose)

class GitResourceBase(Resource, LocalStateResourceMixin, FileResourceMixin):
    def get_local_path_if_any(self):
        return self.local_path

    def get_local_params(self):
        return {
            'local_path':self.local_path
        }

    def validate_subpath_exists(self, subpath):
        """Validate that the subpath is valid within this
        resource. Default implementation checks the local
        filesystem if any. If the resource is remote-only,
        then the subclass should override this method to
        check on the remote side.
        """
        lp = self.get_local_path_if_any()
        if lp is not None:
            path = join(lp, subpath)
            if not exists(path): # use exists() instead of isdir() as subpath could be a file
                raise ConfigurationError("Subpath %s does not exist for resource %s, expecting it at '%s'"%
                                         (subpath, self.name, path))

class GitRepoResource(GitResourceBase):
    def __init__(self, name, role, workspace, remote_origin_url,
                 local_path, branch, read_only):
        super().__init__('git', name, role, workspace)
        self.local_path = local_path
        self.remote_origin_url = remote_origin_url
        self.branch = branch
        self.read_only = read_only

    def get_params(self):
        return {
            'resource_type':self.resource_type,
            'name':self.name,
            'role':self.role,
            'remote_origin_url':self.remote_origin_url,
            'branch':self.branch,
            'read_only':self.read_only
        }

    def results_move_current_files(self, rel_dest_root:str, exclude_files:Set[str],
                                   exclude_dirs_re:Pattern):
        switch_git_branch_if_needed(self.local_path, self.branch, self.verbose)
        validate_git_fat_in_path_if_needed(self.local_path)
        moved_files = move_current_files_local_fs(
            self.name, self.local_path, rel_dest_root,
            exclude_files,
            exclude_dirs_re,
            move_fn=lambda src, dest: git_move_and_add(src, dest, self.local_path,
                                                       self.verbose),
            verbose=self.verbose)
        # If there were no files in the results dir, then we do not
        # create a subdirectory for this snapshot
        if len(moved_files)>0:
            call_subprocess([GIT_EXE_PATH, 'commit',
                             '-m', "Move current results to %s" % rel_dest_root],
                            cwd=self.local_path, verbose=self.verbose)

    def add_results_file(self, temp_path, rel_dest_path):
        """Copy a results file from the temporary location to
        the specified path in the resource. Caller responsible for
        cleanup of temp_path.
        """
        assert exists(temp_path)
        assert self.role==ResourceRoles.RESULTS
        switch_git_branch_if_needed(self.local_path, self.branch, self.verbose)
        abs_dest_path = join(self.local_path, rel_dest_path)
        parent_dir = dirname(abs_dest_path)
        if not exists(parent_dir):
            os.makedirs(parent_dir)
        # Need to use copy instead of move, because /tmp might be in a separate
        # filesystem (see issue #21). Caller will do cleanup of temp file.
        shutil.copyfile(temp_path, abs_dest_path)
        call_subprocess([GIT_EXE_PATH, 'add', rel_dest_path],
                        cwd=self.local_path, verbose=self.verbose)
        call_subprocess([GIT_EXE_PATH, 'commit',
                         '-m', "Added %s" % rel_dest_path],
                        cwd=self.local_path, verbose=self.verbose)

    def snapshot_prechecks(self):
        validate_git_fat_in_path_if_needed(self.local_path)

    def snapshot(self):
        # Todo: handle tags
        commit_changes_in_repo(self.local_path, 'autocommit ahead of snapshot',
                               verbose=self.verbose)
        switch_git_branch_if_needed(self.local_path, self.branch, self.verbose)
        hashval = get_local_head_hash(self.local_path, self.verbose)
        return (hashval, hashval)

    def restore_prechecks(self, hashval):
        rc = call_subprocess_for_rc([GIT_EXE_PATH, 'cat-file', '-e',
                                     hashval+"^{commit}"],
                                    cwd=self.local_path,
                                    verbose=self.verbose)
        if rc!=0:
            raise ConfigurationError("No commit found with hash '%s' in %s" %
                                     (hashval, str(self)))
        if is_a_git_fat_repo(self.local_path):
            import dataworkspaces.third_party.git_fat as git_fat
            self.python2_exe = git_fat.find_python2_exe()
            self.uses_git_fat = True
            validate_git_fat_in_path()
        else:
            self.uses_git_fat = False

    def restore(self, hashval):
        commit_changes_in_repo(self.local_path, 'auto-commit ahead of restore',
                               verbose=self.verbose)
        switch_git_branch_if_needed(self.local_path, self.branch, self.verbose)
        checkout_and_apply_commit(self.local_path, hashval, verbose=self.verbose)
        if self.uses_git_fat:
            # since the restored repo might have different git-fat managed files, we run
            # a pull to get them.
            import dataworkspaces.third_party.git_fat as git_fat
            git_fat.run_git_fat(self.python2_exe, ['pull'], cwd=self.local_path,
                                verbose=self.verbose)



    def push_prechecks(self):
        if self.read_only:
            return
        if is_git_dirty(self.local_path):
            raise ConfigurationError(
                "Git repo at %s has uncommitted changes. Please commit your changes before pushing." %
                self.local_path)
        if is_pull_needed_from_remote(self.local_path, self.branch, self.verbose):
            raise ConfigurationError("Resource '%s' requires a pull from the remote origin before pushing." %
                                     self.name)
        if is_a_git_fat_repo(self.local_path):
            import dataworkspaces.third_party.git_fat as git_fat
            self.python2_exe = git_fat.find_python2_exe()
            self.uses_git_fat = True
        else:
            self.uses_git_fat = False

    def push(self):
        """Push to remote origin, if any"""
        if self.read_only:
            click.echo("Skipping push of resource %s, as it is read-only" % self.name)
            return
        switch_git_branch_if_needed(self.local_path, self.branch, self.verbose)
        call_subprocess([GIT_EXE_PATH, 'push', 'origin', self.branch],
                        cwd=self.local_path, verbose=self.verbose)
        if self.uses_git_fat:
            import dataworkspaces.third_party.git_fat as git_fat
            git_fat.run_git_fat(self.python2_exe, ['push'], cwd=self.local_path,
                                verbose=self.verbose)

    def pull_prechecks(self):
        if is_git_dirty(self.local_path):
            raise ConfigurationError(
                "Git repo at %s has uncommitted changes. Please commit your changes before pulling." %
                self.local_path)
        if is_a_git_fat_repo(self.local_path):
            import dataworkspaces.third_party.git_fat as git_fat
            self.python2_exe = git_fat.find_python2_exe()
            self.uses_git_fat = True
        else:
            self.uses_git_fat = False

    def pull(self):
        """Pull from remote origin, if any"""
        switch_git_branch_if_needed(self.local_path, self.branch, self.verbose)
        call_subprocess([GIT_EXE_PATH, 'pull', 'origin', 'master'],
                        cwd=self.local_path, verbose=self.verbose)
        if self.uses_git_fat:
            import dataworkspaces.third_party.git_fat as git_fat
            git_fat.run_git_fat(self.python2_exe, ['pull'], cwd=self.local_path,
                                verbose=self.verbose)

    def __str__(self):
        return "Git repository %s in role '%s'" % (self.local_path, self.role)

def get_remote_origin(local_path, verbose=False):
    args = [GIT_EXE_PATH, 'config', '--get', 'remote.origin.url']
    if verbose:
        click.echo(" ".join(args) + " [run in %s]" % local_path)
    cp = subprocess.run(args, cwd=local_path, encoding='utf-8',
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if cp.returncode!=0:
        click.echo("Remote origin not found for git repo at %s" % local_path)
        return None
    return cp.stdout.rstrip()

def get_branch_info(local_path, verbose=False):
    data = call_subprocess([GIT_EXE_PATH, 'branch'],
                           cwd=local_path, verbose=verbose)
    current = None
    other = []
    for line in data.split('\n'):
        line = line.strip()
        if len(line)==0:
            continue
        if line.startswith('*'):
            assert current is None
            current = line[2:]
        else:
            other.append(line)
    if current is None:
        raise InternalError("Problem obtaining branch information for local git repo at %s" %
                            local_path)
    else:
        return (current, other)

def switch_git_branch(local_path, branch, verbose):
    try:
        call_subprocess([GIT_EXE_PATH, 'checkout', branch],
                        cwd=local_path, verbose=verbose)
    except Exception as e:
        raise ConfigurationError("Unable to switch git repo at %s to branch %s"
                                 % (local_path, branch)) from e

def switch_git_branch_if_needed(local_path, branch, verbose, ok_if_not_present=False):
    (current, others) = get_branch_info(local_path, verbose)
    if branch==current:
        return
    else:
        if (branch not in others) and (not ok_if_not_present) :
            raise InternalError("Trying to switch to branch %s not in repo at %s"%
                                (branch, others))
        switch_git_branch(local_path, branch, verbose)

class GitLocalPathType(LocalPathType):
    def __init__(self, remote_url, verbose):
        super().__init__()
        self.remote_url = remote_url
        self.verbose = verbose

        def convert(self, value, param, ctx):
            rv = super().convert(value, param, ctx)
            if isdir(rv):
                if not isdir(join(rv, '.git')):
                    self.fail('%s "%s" exists, but is not a git repository' % (self.path_type, rv),
                              param, ctx)
                remote = get_remote_origin(rv, verbose=self.verbose)
                if remote!=self.remote_url:
                    self.fail('%s "%s" is a git repo with remote origin "%s", but dataworkspace has remote "%s"'%
                              (self.path_type, rv), param, ctx)
            return rv


class GitRepoFactory(ResourceFactory):
    def from_command_line(self, role, name, workspace,
                          local_path, branch, read_only):
        """Instantiate a resource object from the add command's
        arguments"""
        workspace.validate_local_path_for_resource(name, local_path)
        validate_git_fat_in_path_if_needed(local_path)
        lpr = realpath(local_path)
        if not is_git_repo(local_path):
            if isinstance(workspace, git_backend.Workspace) and \
               lpr.startswith(realpath(workspace.get_workspace_local_path_if_any())):
                if branch!='master':
                    raise ConfigurationError("Only the branch 'master' is available for resources that are within the workspace's git repository")
                elif read_only:
                    raise ConfigurationError("The --read-only parameter is only valid for separate git repositories, not subdirectories.")
                return GitRepoSubdirFactory().from_command_line(role, name,
                                                                workspace, workspace.batch, workspace.verbose,
                                                                local_path)
            else:
                raise ConfigurationError(local_path + ' is not a git repository')
        remote_origin = get_remote_origin(local_path, verbose=workspace.verbose)
        (current, others) = get_branch_info(local_path, workspace.verbose)
        if branch!=current and branch not in others:
            raise ConfigurationError("Requested branch '%s' is not available for git repository at %s"%
                                     (branch, local_path))
        if is_git_dirty(local_path) and branch!=current:
            click.echo("WARNING: Git repo is currently on branch %s and branch %s was requested. However, the current branch has uncommitted changes. Will skip changing the branch after adding the repo to workspace." % (current, branch))
        else:
            switch_git_branch(local_path, branch, workspace.verbose)
        return GitRepoResource(name, role, workspace,
                               remote_origin, local_path, branch, read_only)

    def from_json(self, params, local_params, workspace):
        """Instantiate a resource object from the parsed resources.json file"""
        assert params['resource_type']=='git'
        return GitRepoResource(params['name'], params['role'],
                               workspace, params['remote_origin_url'],
                               local_params['local_path'], params['branch'],
                               params.get('read_only'))

    def has_local_state(self):
        return True

    def clone(self, params, workspace):
        assert params['resource_type']=='git'
        rname = params['name']
        remote_origin_url = params['remote_origin_url']
        default_local_path = join(workspace.get_workspace_local_path_if_any(), rname) \
                             if workspace.get_workspace_local_path_if_any() is not None \
                             else abspath(expanduser('~'))
        branch = params['branch']
        read_only = params.get('read_only')
        if not workspace.batch:
            # ask the user for a local path
            local_path = \
                click.prompt("Git resource '%s' is being added to your workspace. Where do you want to clone it?"%
                             rname,
                             default=default_local_path, type=GitLocalPathType(remote_origin_url,
                                                                               workspace.verbose))
        else:
            if isdir(default_local_path):
                if not isdir(join(default_local_path, '.git')):
                    raise ConfigurationError("Unable to add resource '%s' as default local path '%s' exists but is not a git repository."%
                                             (rname, default_local_path))
                remote = get_remote_origin(default_local_path, workspace.verbose)
                if remote!=remote_origin_url:
                    raise ConfigurationError("Unable to add resource '%s' as remote origin in local path '%s' is %s, but data workspace has '%s'"%
                                             (rname, default_local_path, remote, remote_origin_url))
            local_path = default_local_path
        parent = dirname(local_path)
        if not exists(self.local_path):
            # cloning a fresh repository
            cmd = [GIT_EXE_PATH, 'clone', remote_origin_url, basename(local_path)]
            call_subprocess(cmd, parent, workspace.verbose)
        else:
            # the repo already exists locally, and we've alerady verified that then
            # remote is correct
            cmd = [GIT_EXE_PATH, 'pull', 'origin', 'master']
            call_subprocess(cmd, local_path, workspace.verbose)
        switch_git_branch_if_needed(local_path, branch, workspace.verbose, ok_if_not_present=True)
        if is_a_git_fat_repo(local_path) and not has_git_fat_been_initialized(local_path):
            import dataworkspaces.third_party.git_fat as git_fat
            python2_exe = git_fat.find_python2_exe()
            git_fat.run_git_fat(python2_exe, ['init'], cwd=local_path, verbose=workspace.verbose)
            git_fat.run_git_fat(python2_exe, ['pull'], cwd=local_path, verbose=workspace.verbose)

        return GitRepoResource(rname, params['role'],
                               workspace, remote_origin_url,
                               local_path, branch, read_only)

    def suggest_name(self, workspace, local_path, branch, read_only):
        return basename(local_path)


def _get_workspace_dir_for_git_backend(workspace):
    """This is used by the git-subdirectory resources, which only work with the
    git backend for the workspace...
    """
    if not isinstance(workspace, git_backend.Workspace):
        raise ConfigurationError("Git subdirectory resources are only supported with the Git workspace backend.")
    workspace_dir = workspace.get_workspace_local_path_if_any()
    assert workspace_dir is not None
    return workspace_dir

class GitRepoResultsSubdirResource(GitResourceBase):
    """Resource for a subdirectory of the workspace for when it is
    in the results role.
    """
    def __init__(self, name, workspace, relative_path):
        super().__init__('git-subdirectory', name, ResourceRoles.RESULTS, workspace)
        self.relative_path = relative_path
        # only valid when workspace has git backend
        self.workspace_dir = _get_workspace_dir_for_git_backend(workspace)
        self.local_path = join(self.workspace_dir,
                               relative_path)

    def get_params(self):
        return {
            'resource_type':self.resource_type,
            'name':self.name,
            'role':self.role,
            'relative_path':self.relative_path
        }

    def results_move_current_files(self, rel_dest_root:str, exclude_files:Set[str],
                                   exclude_dirs_re:Pattern):
        validate_git_fat_in_path_if_needed(self.workspace_dir)
        moved_files = move_current_files_local_fs(
            self.name, self.local_path, rel_dest_root,
            exclude_files,
            exclude_dirs_re,
            move_fn=lambda src, dest: git_move_and_add(src, dest, self.local_path,
                                                       self.workspace.verbose),
            verbose=self.workspace.verbose)
        # If there were no files in the results dir, then we do not
        # create a subdirectory for this snapshot
        if len(moved_files)>0:
            call_subprocess([GIT_EXE_PATH, 'commit', '--only', self.relative_path,
                             '-m', "Move current results to %s" % rel_dest_root],
                            cwd=self.workspace_dir,
                            verbose=self.workspace.verbose)

    def add_results_file(self, temp_path, rel_dest_path):
        """Move a results file from the temporary location to
        the specified path in the resource. Caller responsible
        for cleanup of temp_path
        """
        assert exists(temp_path)
        abs_dest_path = join(self.local_path, rel_dest_path)
        parent_dir = dirname(abs_dest_path)
        if not exists(parent_dir):
            os.makedirs(parent_dir)
        # Need to use copy instead of move, because /tmp might be in a separate
        # filesystem (see issue #21). Caller will do cleanup of temp file.
        shutil.copyfile(temp_path, abs_dest_path)
        rel_path_in_repo = join(self.relative_path, rel_dest_path)
        call_subprocess([GIT_EXE_PATH, 'add', rel_path_in_repo],
                        cwd=self.workspace_dir, verbose=self.verbose)
        call_subprocess([GIT_EXE_PATH, 'commit',
                         '-m', "Added %s" % rel_path_in_repo],
                        cwd=self.workspace_dir, verbose=self.verbose)

    def snapshot_prechecks(self):
        validate_git_fat_in_path_if_needed(self.workspace_dir)

    def snapshot(self):
        # The subdirectory hash is used for comparison and the head
        # hash used for restoring
        return (get_subdirectory_hash(self.workspace_dir, self.relative_path,
                                      verbose=self.verbose),
                get_local_head_hash(self.workspace_dir, verbose=self.verbose))


    def restore_prechecks(self, hashval):
        raise ConfigurationError("Git subdirectory resource '%s' should not be included in restore set"%
                                 self.name)

    def restore(self, hashval):
        raise InternalError("Should never call restore on a git subdirectory resource (%s)"%
                            self.name)


    def push_prechecks(self):
        if not exists(self.local_path):
            raise ConfigurationError("Missing directory %s for resource %s"%
                                     (self.local_path, self.name))
        if is_git_dirty(self.workspace_dir):
            raise ConfigurationError(
                "Git repo at %s has uncommitted changes. Please commit your changes before pushing." %
                self.workspace_dir)
        if is_pull_needed_from_remote(self.workspace_dir, 'master', self.verbose):
            raise ConfigurationError("Resource '%s' requires a pull from the remote origin before pushing." %
                                     self.name)

    def push(self):
        """Push to remote origin, if any"""
        pass # push will happen at workspace level

    def pull_prechecks(self):
        if is_git_dirty(self.local_path):
            raise ConfigurationError(
                "Git repo at %s has uncommitted changes. Please commit your changes before pulling." %
                self.workspace_dir)

    def pull(self):
        """Pull from remote origin, if any"""
        pass # pull will happen at workspace level

    def __str__(self):
        return "Git repository subdirectory %s in role '%s'" % (self.relative_path, self.role)


class GitRepoSubdirResource(GitResourceBase):
    """Resource for a subdirectory of the workspace for when it is NOT
    in the results role.
    """
    def __init__(self, name, role, workspace, relative_path):
        assert role != ResourceRoles.RESULTS
        super().__init__('git-subdirectory', name, role, workspace)
        self.relative_path = relative_path
        self.local_path = join(_get_workspace_dir_for_git_backend(workspace), relative_path)

    def get_params(self):
        return {
            'resource_type':self.resource_type,
            'name':self.name,
            'role':self.role,
            'relative_path':self.relative_path
        }

    def results_move_current_files(self, rel_dest_root:str, exclude_files:Set[str],
                                   exclude_dirs_re:Pattern):
        raise InternalError("results_move_current_files should not be called for %s" % self.__class__.__name__)

    def add_results_file(self, temp_path, rel_dest_path):
        """Copy a results file from the temporary location to
        the specified path in the resource. Caller responsible for cleanup.
        """
        raise InternalError("add_results_file should not be called for %s" %
                            self.__class__.__name__)

    def snapshot_prechecks(self):
        validate_git_fat_in_path_if_needed(self.workspace_dir)

    def snapshot(self):
        # Todo: handle tags
        commit_changes_in_repo_subdir(self.workspace_dir, self.relative_path, 'autocommit ahead of snapshot',
                                      verbose=self.verbose)
        return (get_subdirectory_hash(self.workspace_dir, self.relative_path,
                                      verbose=self.verbose),
                get_local_head_hash(self.workspace_dir, verbose=self.verbose))

    def restore_prechecks(self, hashval):
        validate_git_fat_in_path_if_needed(self.workspace_dir)
        rc = call_subprocess_for_rc([GIT_EXE_PATH, 'cat-file', '-e',
                                     hashval+"^{commit}"],
                                    cwd=self.workspace_dir,
                                    verbose=self.verbose)
        if rc!=0:
            raise ConfigurationError("No commit found with hash '%s' in %s" %
                                     (hashval, str(self)))

    def restore(self, hashval):
        commit_changes_in_repo_subdir(self.workspace_dir, self.relative_path,
                                      'auto-commit ahead of restore',
                                      verbose=self.verbose)
        checkout_subdir_and_apply_commit(self.workspace_dir, self.relative_path, hashval, verbose=self.verbose)


    def push_prechecks(self):
        if not exists(self.local_path):
            raise ConfigurationError("Missing directory %s for resource %s"%
                                     (self.local_path, self.name))
        if is_git_dirty(self.local_path):
            raise ConfigurationError(
                "Git repo at %s has uncommitted changes. Please commit your changes before pushing." %
                self.local_path)
        if is_pull_needed_from_remote(self.local_path, 'master', self.verbose):
            raise ConfigurationError("Resource '%s' requires a pull from the remote origin before pushing." %
                                     self.name)

    def push(self):
        """Push to remote origin, if any"""
        pass # push will happen at workspace level
        # actions.call_subprocess([GIT_EXE_PATH, 'push', 'origin', 'master'],
        #                         cwd=self.local_path, verbose=self.verbose)

    def pull_prechecks(self):
        if is_git_dirty(self.local_path):
            raise ConfigurationError(
                "Git repo at %s has uncommitted changes. Please commit your changes before pulling." %
                self.local_path)

    def pull(self):
        """Pull from remote origin, if any"""
        pass # pull will happen at workspace level
        # actions.call_subprocess([GIT_EXE_PATH, 'pull', 'origin', 'master'],
        #                         cwd=self.local_path, verbose=self.verbose)

    def __str__(self):
        return "Git repository subdirectory %s in role '%s'" % (self.relative_path, self.role)

CONFIRM_SUBDIR_MSG=\
"The subdirectory %s does not currently exist, but must be a part of the workspace's git repo in order"+\
" to be used as a resource. Do you want it to be created and added to git?"

def create_results_subdir(workspace_dir, full_path, relative_path, role, verbose):
    os.makedirs(full_path)
    with open(join(full_path, 'README.txt'), 'w') as f:
        f.write("This directory is for %s.\n" % RESOURCE_ROLE_PURPOSES[role])
        f.write("This README file ensures the directory is added to the git repository, as git does not support empty directories.\n")
    call_subprocess([GIT_EXE_PATH, 'add', relative_path],
                    cwd=workspace_dir, verbose=verbose)
    call_subprocess([GIT_EXE_PATH, 'commit', '-m',
                     'Add %s to repo for storing results'%relative_path],
                    cwd=workspace_dir, verbose=verbose)
    click.echo("Added %s to git repository" % relative_path)


class GitRepoSubdirFactory(ResourceFactory):
    """This is a version of a git repo resource where we are just
    storing in a subdirectory of a repo rather than the full repo.
    This is currently only valid if we are storing as a subdir of the
    main data workspace repo.
    """
    def from_command_line(self, role, name, workspace,
                          local_path, confirm_subdir_create=True):
        """Instantiate a resource object from the add command's
        arguments"""
        if is_git_repo(local_path):
            raise InternalError("Local path '%s'is a git repo, should not be using GitRepoSubdirFactory"%
                                local_path)
        lpr = realpath(local_path)
        workspace_dir = _get_workspace_dir_for_git_backend(workspace)
        wdr = realpath(workspace_dir)
        if not lpr.startswith(wdr):
            raise ConfigurationError("Git subdirectories can only be used as resources when under the workspace repo.")
        relative_path = lpr[len(wdr)+1:]
        if not exists(local_path):
            if not confirm_subdir_create:
                create_results_subdir(workspace_dir, local_path, relative_path,
                                      role, workspace.verbose)
            elif not workspace.batch:
                click.confirm(CONFIRM_SUBDIR_MSG%relative_path, abort=True)
                create_results_subdir(workspace_dir, local_path, relative_path,
                                      role, workspace.verbose)
            else:
                raise ConfigurationError("Cannot create a resource from a git subdirectory if the directory does not already exist.")
        if role==ResourceRoles.RESULTS:
            return GitRepoResultsSubdirResource(name, workspace, relative_path)
        else:
            return GitRepoSubdirResource(name, role, workspace, relative_path)

    def from_json(self, params, local_params, workspace):
        """Instantiate a resource object from the parsed resources.json file"""
        assert params['resource_type']=='git-subdirectory'
        if params['role']==ResourceRoles.RESULTS:
            return GitRepoResultsSubdirResource(params['name'],
                                                workspace,
                                                params['relative_path'])
        else:
            return GitRepoSubdirResource(params['name'], params['role'],
                                         workspace, params['relative_path'])

    def clone(self, params, workspace):
        assert params['resource_type']=='git-subdirectory'
        rname = params['name']
        role = params['role']
        relative_path = params['relative_path']
        if not isinstance(workspace, git_backend.Workspace):
            raise ConfigurationError("Git subdirectory resources are only supported with the Git workspace backend.")
        workspace_dir = workspace.get_local_path_if_any()
        assert workspace_dir is not None
        local_path = join(workspace_dir, relative_path)
        if not exists(local_path):
            # this subdirectory most have been created in the remote
            # resource. We can just wait for the "git pull" to populate the
            # the contents, but will create a placeholder so our checks pass.
            os.mkdir(local_path)
        if role==ResourceRoles.RESULTS:
            return GitRepoResultsSubdirResource(rname, workspace, relative_path)
        else:
            return GitRepoSubdirResource(rname, role, workspace, relative_path)

    def has_local_state(self) -> bool:
        return True

    def suggest_name(self, local_path, *args):
        return basename(local_path)

