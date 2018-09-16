#!/usr/bin/env python3
"""
Command-line tool for data workspaces
"""

__all__ = ['cli']
import sys
import click
import os
from os.path import isdir, join, dirname, abspath, expanduser, basename, curdir
from argparse import Namespace

from .commands.init import init_command
from .commands.add import add_command
from .commands.snapshot import snapshot_command
from .commands.restore import restore_command
from .resources.resource import RESOURCE_ROLE_CHOICES, ResourceRoles
from .errors import BatchModeError

CURR_DIR = abspath(expanduser(curdir))
CURR_DIRNAME=basename(CURR_DIR)

# we are going to store the verbose mode
# in a global here and wrap it in a function
# so that we can access it from __main__.
VERBOSE_MODE=False
def is_verbose_mode():
    global VERBOSE_MODE
    return VERBOSE_MODE

def _find_containing_workspace():
    """For commands that execute in the context of a containing
    workspace, find the nearest containging workspace and return
    its absolute path. If none is found, return None.
    """
    curr_base = CURR_DIR
    while curr_base != '/':
        if isdir(join(curr_base, '.dataworkspace')) and os.access(curr_base, os.W_OK):
            return curr_base
        else:
            curr_base = dirname(curr_base)
    return None

DWS_PATHDIR=_find_containing_workspace()

class WorkspaceDirParamType(click.ParamType):
    name = 'workspace-directory'

    def convert(self, value, param, ctx):
        path = abspath(expanduser(value))
        wspath = join(path, '.dataworkspace')
        if not isdir(path):
            self.fail("Directory '%s' does not exist" % value, param, ctx)
        elif not isdir(wspath):
            self.fail("No .dataworkspace directory found under '%s'. Did you run 'dws init'?"%
                      value, param, ctx)
        else:
            return path

WORKSPACE_PARAM = WorkspaceDirParamType()

class DirectoryParamType(click.ParamType):
    name = 'directory'

    def convert(self, value, param, ctx):
        path = abspath(expanduser(value))
        if not isdir(path):
            self.fail("Directory '%s' does not exist" % value, param, ctx)
        else:
            return path

DIRECTORY_PARAM = DirectoryParamType()


class RoleParamType(click.ParamType):
    name = 'role (one of %s)' % ', '.join(RESOURCE_ROLE_CHOICES)

    def convert(self, value, param, ctx):
        value = value.lower()
        if value in (ResourceRoles.SOURCE_DATA_SET, 's'):
            return ResourceRoles.SOURCE_DATA_SET
        elif value in (ResourceRoles.INTERMEDIATE_DATA, 'i'):
            return ResourceRoles.INTERMEDIATE_DATA
        elif value in (ResourceRoles.CODE, 'c'):
            return ResourceRoles.CODE
        elif value in (ResourceRoles.RESULTS, 'r'):
            return ResourceRoles.RESULTS
        else:
            self.fail("Invalid resource role. Must be one of: %s" %
                      ', '.join(RESOURCE_ROLE_CHOICES))
ROLE_PARAM = RoleParamType()

@click.group()
@click.option('-b', '--batch', default=False, is_flag=True,
              help="Run in batch mode, never ask for user inputs.")
@click.option('--verbose', default=False, is_flag=True,
              help="Print extra debugging information and ask for confirmation before running actions.")
@click.pass_context
def cli(ctx, batch, verbose):
    ctx.obj = Namespace()
    ctx.obj.batch = batch
    ctx.obj.verbose = verbose
    global VERBOSE_MODE
    VERBOSE_MODE = verbose


@click.command()
@click.argument('name', default=CURR_DIRNAME)
@click.pass_context
def init(ctx, name):
    """Initialize a new workspace"""
    init_command(name, **vars(ctx.obj))


cli.add_command(init)

@click.command()
@click.pass_context
def clone(ctx):
    """Initialize a workspace from a remote source"""
    pass

cli.add_command(clone)


# The add command has subcommands for each resource type.
# This should be dynamically extensible, but we will hard
# code things for now.
@click.group()
@click.option('--workspace-dir', type=WORKSPACE_PARAM, default=DWS_PATHDIR)
@click.pass_context
def add(ctx, workspace_dir):
    """Add a data collection to the workspace"""
    ns = ctx.obj
    if workspace_dir is None:
        if ns.batch:
            raise BatchModeError("--workspace-dir")
        else:
            workspace_dir = click.prompt("Please enter the workspace root dir",
                                         type=WORKSPACE_PARAM)
        
    ns.workspace_dir = workspace_dir

cli.add_command(add)

@click.command(name='local-files')
@click.option('--role', type=ROLE_PARAM)
@click.option('--name', type=str, default=None,
              help="Short name for this resource")
@click.argument('path', type=DIRECTORY_PARAM)
@click.pass_context
def local_files(ctx, role, name, path):
    """Local file directory (not managed by git)"""
    ns = ctx.obj
    if role is None:
        if ns.batch:
            raise BatchModeError("--role")
        else:
            role = click.prompt("Please enter a role for this resource, one of [s]ource-data, [i]ntermediate-data, [c]ode, or [r]esults", type=ROLE_PARAM)
    add_command('file', role, name, ns.workspace_dir, ns.batch, ns.verbose, path)

add.add_command(local_files)

@click.command()
@click.option('--role', type=ROLE_PARAM)
@click.option('--name', type=str, default=None,
              help="Short name for this resource")
@click.argument('path', type=DIRECTORY_PARAM)
@click.pass_context
def git(ctx, role, name, path): 
    """Local git repository"""
    ns = ctx.obj
    if role is None:
        if ns.batch:
            raise BatchModeError("--role")
        else:
            role = click.prompt("Please enter a role for this resource, one of [s]ource-data, [i]ntermediate-data, [c]ode, or [r]esults", type=ROLE_PARAM)
    add_command('git', role, name, ns.workspace_dir, ns.batch, ns.verbose, path)

add.add_command(git)

@click.command()
@click.option('--workspace-dir', type=WORKSPACE_PARAM, default=DWS_PATHDIR)
@click.option('--message', '-m', type=str, default='',
              help="Message describing the snapshot")
@click.argument('tag', type=str, default=None, required=False)
@click.pass_context
def snapshot(ctx, workspace_dir, message, tag):
    """Take a snapshot of the current workspace's state"""
    ns = ctx.obj
    snapshot_command(workspace_dir, ns.batch, ns.verbose, tag, message)

cli.add_command(snapshot)

@click.command()
@click.option('--workspace-dir', type=WORKSPACE_PARAM, default=DWS_PATHDIR)
@click.option('--only', type=str, default=None,
              help="Comma-separated list of resource names that you wish to revert to the specified snapshot. The rest will be left as-is.")
@click.option('--leave', type=str, default=None,
              help="Comma-separated list of resource names that you wish to leave in their current state. The rest will be restored to the specified snapshot.")
@click.option('--no-new-snapshot', is_flag=True, default=False,
              help="By default, a new snapshot will be taken if the restore leaves the "+
                   "workspace in a different state than the requested shapshot (e.g. due "+
                   "to --only or --leave or added resources). If --no-new-snapshot is "+
                   "specified, we adjust the individual resource states without taking a new snapshot.")
@click.argument('tag_or_hash', type=str, default=None, required=True)
@click.pass_context
def restore(ctx, workspace_dir, only, leave, no_new_snapshot, tag_or_hash):
    """Restore the workspace to a prior state"""
    ns = ctx.obj
    if (only is not None) and (leave is not None):
        raise click.BadOptionUsage(message="Please specify either --only or --leave, but not both")
    restore_command(workspace_dir, ns.batch, ns.verbose, tag_or_hash,
                    only=only, leave=leave, no_new_snapshot=no_new_snapshot)

cli.add_command(restore)


if __name__=='__main__':
    cli()
    sys.exit(0)

cli.add_command(snapshot)
