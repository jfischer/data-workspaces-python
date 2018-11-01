import tempfile
from os.path import join
import tarfile
import json

import click

import dataworkspaces.commands.actions as actions
from dataworkspaces.resources.resource import \
    CurrentResources, get_resource_from_json_remote
from .init import get_config_file_path
from .add import UpdateLocalParams, add_local_dir_to_gitignore_if_needed
from .push import get_resources_to_process
from dataworkspaces.resources.git_resource import is_git_dirty
from dataworkspaces.errors import ConfigurationError



class PullResource(actions.Action):
    def __init__(self, ns, verbose, r):
        super().__init__(ns, verbose)
        self.r = r
        r.pull_prechecks()

    def run(self):
        click.echo("Pulling resource %s..." % self.r.name)
        self.r.pull()

    def __str__(self):
        return "Pull state of resource '%s' to origin" % self.r.name

class AddRemoteResource(actions.Action):
    def __init__(self, ns, verbose, workspace_dir, resource_json):
        super().__init__(ns, verbose)
        self.r = get_resource_from_json_remote(resource_json, workspace_dir, batch_verbose)
        self.r.add_prechecks() # XXX should there be different prechecks for adding a remote?

    def run(self):
        self.r.add_from_remote()

    def __str__(self):
        return "Add remote resource %s to local dataworkspace" % self.r.name


class PullWorkspace(actions.Action):
    def __init__(self, ns, verbose, workspace_dir):
        super().__init__(ns, verbose)
        self.workspace_dir = workspace_dir
        if is_git_dirty(workspace_dir):
            raise ConfigurationError("Data workspace metadata repo at %s has uncommitted changes. Please commit before pulling." %
                                     workspace_dir)

    def run(self):
        click.echo("Pulling workspace...")
        actions.call_subprocess([actions.GIT_EXE_PATH, 'pull', 'origin', 'master'],
                                cwd=self.workspace_dir, verbose=self.verbose)

    def __str__(self):
        return "Pull state of data workspace metadata to origin"

def get_json_file_from_remote(relpath, workspace_dir, batch, verbose):
    try:
        with tempfile.TemporaryDirectory() as tdir:
            tarpath = join(tdir, 'test.tgz')
            cmd = [actions.GIT_EXE_PATH, 'archive', '-o', tarpath, '--remote=origin',
                   'refs/heads/master', relpath]
            actions.call_subprocess(cmd, workspace_dir, verbose)
            with tarfile.open(name=tarpath) as tf:
                tf.extract(relpath, path=tdir)
            with open(join(tdir, relpath), 'r') as f:
                return json.load(f)
    except Exception as e:
        raise ConfigurationError("Problem retrieving file %s from remote"%relpath) from e

def get_resources_from_remote(workspace_dir, batch, verbose):
    """We want to read the resources.json file from the remote without pulling or fetching.
    We can do that by creating an archive with just the resources.json file.
    """
    return get_json_file_from_remote('.dataworkspace/resources.json', workspace_dir, batch, verbose)


def pull_command(workspace_dir, batch=False, verbose=False,
                 only=None, skip=None, only_workspace=False):
    plan = []
    ns = actions.Namespace()
    ns.local_params_json = {}
    if not only_workspace:
        current_resources = CurrentResources.read_current_resources(workspace_dir,
                                                                    batch, verbose)
        remote_resources_json = get_resources_from_remote(workspace_dir, batch, verbose)
        for name in get_resources_to_process(current_resources, only, skip):
            r = current_resources.by_name[name]
            plan.append(PullResource(ns, verbose, r))
        plan.append(PullWorkspace(ns, verbose, workspace_dir))
        gitignore_path = None
        for resource_json in remote_resources_json:
            if current_resources.is_a_current_name(resource_json['name']):
                continue
            # resouce not local, was added to the remote workspace
            add_remote_action = AddRemoteResource(ns, verbose, workspace_dir, resource_json)
            plan.append(add_remote_action)
            plan.append(UpdateLocalParams(ns, verbose, add_remote_action.r, workspace_dir))
            add_to_gi = add_local_dir_to_gitignore_if_needed(ns, verbose, add_remote_action.r,
                                                             workspace_dir)
            if add_to_gi:
                plan.append(add_to_gi)
                gitignore_path = add_to_gi.gitignore_path
        if gitignore_path:
            plan.append(actions.GitAdd(ns, verbose, workspace_dir, [gitignore_path]))
            plan.append(actions.GitCommit(ns, verbose, workspace_dir, "Added new resources to gitignore"))
    else:
        plan.append(PullWorkspace(ns, verbose, workspace_dir))
    actions.run_plan(plan, "pull state from origins",
                     "pulled state from origins", batch=batch, verbose=verbose)




