import json
from os.path import join, exists
from collections import namedtuple

import click

import dataworkspaces.commands.actions as actions
from dataworkspaces.errors import ConfigurationError, InternalError
from dataworkspaces.resources.resource import CurrentResources, SnapshotResources
from .snapshot import TakeResourceSnapshot, AppendSnapshotHistory

class RestoreResource(actions.Action):
    def __init__(self, verbose, resource, snapshot_resources):
        super().__init__(verbose)
        self.resource = resource
        self.hashval = snapshot_resources.url_to_hashval[resource.url]
        self.resource.restore_prechecks(self.hashval)

    def run(self):
        self.resource.restore(self.hashval)

    def __str__(self):
        return "Run restore actions for %s" % str(self.resource)

class SkipResource(actions.Action):
    def __init__(self, verbose, resource, reason):
        super().__init__(verbose)
        self.resource = resource
        self.reason = reason

    def run(self):
        pass

    def __str__(self):
        return 'Skipping resource %s, %s' % (str(self.resource), self.reason)

class AddResourceToSnapshot(actions.Action):
    def __init__(self, verbose, resource, snapshot_resources):
        super().__init__(verbose)
        self.resource = resource
        self.snapshot_resources = snapshot_resources
        # A given resource should resolve to a unique URL, so this is the best way
        # to check for duplication.
        if resource.url in snapshot_resources.urls:
            raise ConfigurationError("A resource with url '%s' already in snapshot" % resource.url)

    def run(self):
        self.snapshot_resources.add_resource(self.resource)
        self.snapshot_resources.write_snapshot_resources()

    def __str__(self):
        return "Add '%s' to resources.json file" % str(self.resource)

class WriteRevisedSnapshotFile(actions.Action):
    def __init__(self, verbose, workspace_dir, map_of_hashes, snapshot_resources):
        self.verbose = verbose
        self.workspace_dir = workspace_dir
        self.map_of_hashes = map_of_hashes
        self.snapshot_resources = snapshot_resources
        self.snapshot_hash = None
        self.snapshot_filename = None

    def run(self):
        def write_fn(tempfile):
            self.snapshot_resources.write_revised_snapshot_manifest(tempfile,
                                                                   self.map_of_hashes)
        (self.snapshot_hash, self.snapshot_filename) = \
            actions.write_and_hash_file(
                write_fn,
                join(self.workspace_dir,
                     ".dataworkspace/snapshots/snapshot-<HASHVAL>.json"),
                self.verbose)

    def __str__(self):
        return 'Create and hash snapshot file'

class WriteRevisedResourceFile(actions.Action):
    def __init__(self, verbose, snapshot_resources):
        self.verbose = verbose
        self.snapshot_resources = snapshot_resources

    def run(self):
        self.snapshot_resources.write_current_resources()

    def __str__(self):
        return "Write revised resources.json file"


def process_names(current_names, snapshot_names, only=None, leave=None):
    """Based on what we have currently, what's in the snapshot, and the
    --only, --leave, and --ignore-dropped command line options, figure out what we should
    do for each resource.
    """
    print("current_names = %s" % ', '.join(sorted(current_names))) # XXX
    print("snapshot_names = %s" % ', '.join(sorted(snapshot_names))) # XXX
    all_names = snapshot_names.union(current_names)
    names_to_restore = snapshot_names.intersection(current_names)
    names_to_add = snapshot_names.difference(current_names)
    names_to_leave = current_names.difference(snapshot_names)

    if only is not None:
        only_names = only.split(',')
        for name in only_names:
            if name not in all_names:
                raise click.UsageError("No resource in '%s' exists in current or restored workspaces"
                                       % name)
        for name in all_names.difference(only_names):
            # Names not in only 
            if name in names_to_restore:
                names_to_restore.remove(name)
                names_to_leave.add(name)

    if leave is not None:
        leave_names = leave.split(',')
        for name in leave_names:
            if name not in all_names:
                raise click.UsageError("No resource in '%s' exists in current or restored workspaces"
                                       % name)
            elif name in names_to_restore:
                names_to_restore.remove(name)
                names_to_leave.add(name)
    return (sorted(names_to_restore), sorted(names_to_add), sorted(names_to_leave))

def restore_command(workspace_dir, batch, verbose, tag_or_hash,
                    only=None, leave=None, no_new_snapshot=False):
    # First, find the history entry
    sh_file = join(workspace_dir, '.dataworkspace/snapshots/snapshot_history.json')
    with open(sh_file, 'r') as f:
        sh_data = json.load(f)
    is_hash = actions.is_a_git_hash(tag_or_hash)
    found = False
    for snapshot in sh_data:
        if is_hash and snapshot['hash']==tag_or_hash:
            found = True
            break
        elif (not is_hash) and snapshot['tag']==tag_or_hash:
            found = True
            break
    if not found:
        if is_hash:
            raise ConfigurationError("Did not find a snapshot corresponding to '%s' in history" % tag_or_hash)
        else:
            raise ConfigurationError("Did not find a snapshot corresponding to tag '%s' in history" % tag_or_hash)
    snapshot_resources = SnapshotResources.read_shapshot_manifest(snapshot['hash'], workspace_dir, batch, verbose)
    current_resources = CurrentResources.read_current_resources(workspace_dir, batch, verbose)
    original_current_resource_names = current_resources.get_names()
    (names_to_restore, names_to_add, names_to_leave) = \
        process_names(original_current_resource_names, snapshot_resources.get_names(), only, leave)
    print("names_to_restore: %s, names_to_add: %s, names_to_leave: %s" %
          (repr(names_to_restore), repr(names_to_add), repr(names_to_leave)))
    plan = []
    create_new_hash = False
    map_of_hashes = {}
    for name in names_to_restore:
        # resources in both current and restored, just need to call restore
        plan.append(RestoreResource(verbose, current_resources.by_name[name],
                                    snapshot_resources))
    for name in names_to_add:
        # These are resources which are in the restored snapshot, but not the
        # current resources. We'll grab the resource objects from snapshot_resources
        r = snapshot_resources.by_name[name]
        plan.append(RestoreResource(verbose, r, snapshot_resources))
    for name in names_to_leave:
        # These resources are only in the current resource list or explicitly left out.
        r = current_resources.by_name[name]
        # if we are adding a current resource to the restored snapshot, we actually
        # have to snapshot the resource itself.
        r = current_resources.by_name[name]
        if not snapshot_resources.is_a_current_name(name):
            plan.append(AddResourceToSnapshot(verbose, r, snapshot_resources))
        if not no_new_snapshot:
            plan.append(TakeResourceSnapshot(verbose, r, map_of_hashes))
            create_new_hash = True
    need_to_write_resources_file = \
        original_current_resource_names!=snapshot_resources.get_names()
    if create_new_hash:
        write_revised = WriteRevisedSnapshotFile(verbose, workspace_dir, map_of_hashes,
                                                 snapshot_resources)
        plan.append(write_revised)
        history_action = AppendSnapshotHistory(verbose, workspace_dir, None,
                                               "Revert creating a new hash",
                                               lambda:write_revised.snapshot_hash)
        plan.append(history_action)
        if need_to_write_resources_file:
            plan.append(WriteRevisedResourceFile(verbose, snapshot_resources))
            plan.append(actions.GitAddDeferred(workspace_dir,
                                               lambda:[write_revised.snapshot_filename,
                                                       history_action.snapshot_history_file,
                                                       snapshot_resources.resource_file],
                                               verbose))
        else:
            plan.append(actions.GitAddDeferred(workspace_dir,
                                               lambda:[write_revised.snapshot_filename,
                                                       history_action.snapshot_history_file],
                                               verbose))
    elif need_to_write_resources_file:
        plan.append(actions.GitAdd(workspace_dir,
                                   [snapshot_resources.resource_file],
                                   verbose))

    tagstr = ', tag=%s' % snapshot['tag'] if snapshot['tag'] else ''
    if create_new_hash:
        desc = "Partial restore of snapshot %s%s, resulting in a new snapshot"% \
                (snapshot['hash'], tagstr)
        commit_msg_fn = lambda: desc + " " + (lambda h:h.snapshot_hash)(write_revised)
    else:
        desc = "Restore snapshot %s%s" % (snapshot['hash'], tagstr)
        commit_msg_fn = lambda: desc

    if need_to_write_resources_file or create_new_hash:
        plan.append(actions.GitCommit(workspace_dir,
                                      message=commit_msg_fn,
                                      verbose=verbose))
    click.echo(desc)
    def fmt_rlist(rnames):
        if len(rnames)>0:
            return ', '.join(rnames)
        else:
            return 'None'
    click.echo("  Resources to restore: %s" % fmt_rlist(names_to_restore))
    click.echo("  Resources to add: %s" % fmt_rlist(names_to_add))
    click.echo("  Resources to leave: %s" % fmt_rlist(names_to_leave))
    if (not verbose) and (not batch):
        # Unless in batch mode, we always want to ask for confirmation
        # If not in verbose, do it here. In verbose, we'll ask after
        # we print the plan.
        resp = input("Should I perform this restore? [Y/n]")
        if resp.lower()!='y' and resp!='':
            raise UserAbort()
    actions.run_plan(plan, 'run this restore', 'run restore', batch, verbose)
    if create_new_hash:
        click.echo("New snapshot is %s." % write_revised.snapshot_hash)
    
