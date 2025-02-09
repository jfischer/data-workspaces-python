"""Utilities for running tests of the dws command line
"""

import shutil
import subprocess
import filecmp
import json
from os.path import join, abspath, expanduser, exists
import os
import unittest
import sys
import json
import gzip
import configparser

TEMPDIR=abspath(expanduser(__file__)).replace('.py', '_data')
WS_DIR=join(TEMPDIR,'workspace')
WS_ORIGIN=join(TEMPDIR, 'workspace_origin.git')
OTHER_WS=join(TEMPDIR, 'workspace2')

try:
    import dataworkspaces
except ImportError:
    sys.path.append(os.path.abspath(".."))

from dataworkspaces.utils.git_utils import GIT_EXE_PATH
from dataworkspaces.utils.subprocess_utils import find_exe, call_subprocess_for_rc

class HelperMethods:
    def _run_dws(self, dws_args, cwd=WS_DIR, env=None):
        command = self.dws + ' --verbose --batch '+ ' '.join(dws_args)
        print(command + (' [%s]' % cwd))
        r = subprocess.run(command, cwd=cwd, shell=True, env=env)
        r.check_returncode()

    def _run_dws_with_input(self, dws_args, dws_input, cwd=WS_DIR, env=None):
        command = self.dws + ' --verbose '+ ' '.join(dws_args)
        print(command + (' [%s]' % cwd))
        print(" Input will be %s" % repr(dws_input))
        r = subprocess.run(command, cwd=cwd, input=dws_input, shell=True, env=env,
                           encoding='utf-8')
        r.check_returncode()

    def _run_git(self, git_args, cwd=WS_DIR):
        args = [GIT_EXE_PATH]+git_args
        print(' '.join(args) + (' [%s]' % cwd))
        r = subprocess.run(args, cwd=cwd)
        r.check_returncode()

    def _add_api_resource(self, name, role='source-data', cwd=WS_DIR):
        self._run_dws(['add', 'api-resource', '--role', role,
                       '--name', name], cwd=cwd)

    def _assert_files_same(self, f1, f2):
        self.assertTrue(exists(f1), "Missing file %s" % f1)
        self.assertTrue(exists(f2), "Missing file %s" % f2)
        self.assertTrue(filecmp.cmp(f1, f2, shallow=False),
                        "Files %s and %s are different" % (f1, f2))

    def _assert_file_contents(self, filepath, expected_contents):
        with open(filepath, 'r') as f:
            data = f.read()
        self.assertEqual(expected_contents, data, "File %s does not contain expected data"%filepath)

    def _assert_file_git_tracked(self, rel_path, repo_dir=WS_DIR):
        rc = call_subprocess_for_rc([GIT_EXE_PATH, 'ls-files', '--error-unmatch',
                                     rel_path],
                                    cwd=repo_dir, verbose=True)
        self.assertEqual(0, rc,
                         "File %s should be in git repo %s, but it was not"%
                         (rel_path, repo_dir))

    def _assert_file_not_git_tracked(self, rel_path, repo_dir=WS_DIR):
        rc = call_subprocess_for_rc([GIT_EXE_PATH, 'ls-files', '--error-unmatch',
                                     rel_path],
                                    cwd=repo_dir, verbose=True)
        self.assertNotEqual(0, rc,
                            "File %s should not be in git repo %s, but it was"%
                            (rel_path, repo_dir))

    def _get_resource_set(self, workspace_dir):
        resource_file = join(workspace_dir, '.dataworkspace/resources.json')
        with open(resource_file, 'r') as f:
            data = json.load(f)
        names = set()
        for obj in data:
            names.add(obj['name'])
        return names


class BaseCase(HelperMethods, unittest.TestCase):
    """utilities to set up an environment that has two copies of a workspace
    and a central bare git repo as the origin.
    """
    def setUp(self):
        if os.path.exists(TEMPDIR):
            shutil.rmtree(TEMPDIR)
        os.mkdir(TEMPDIR)
        os.mkdir(WS_DIR)
        self.dws=find_exe("dws", "Make sure you have enabled your python virtual environment")

    def tearDown(self):
        if os.path.exists(TEMPDIR):
            shutil.rmtree(TEMPDIR)

    def _setup_initial_repo(self, create_resources=None, scratch_dir=None, hostname=None):
        init_cmd = ['init']
        if create_resources is not None:
           init_cmd.append('--create-resources='+create_resources)
        if scratch_dir is not None:
            init_cmd.append('--scratch-directory='+scratch_dir)
        if hostname is not None:
            init_cmd.append('--hostname='+hostname)
        self._run_dws(init_cmd, cwd=WS_DIR)
        self._run_git(['init', '--bare', 'workspace_origin.git'],
                      cwd=TEMPDIR)
        self._run_git(['remote', 'add', 'origin', WS_ORIGIN], cwd=WS_DIR)
        self._run_dws(['push'], cwd=WS_DIR)

    def _clone_second_repo(self):
        self._run_dws(['clone', WS_ORIGIN, 'workspace2'], cwd=TEMPDIR)



class SimpleCase(HelperMethods, unittest.TestCase):
    """utilities to set up an environment that has a single workspace with
    no origin or remote. This is for tests that are not involved
    in syncing of workspaces.
    """
    def setUp(self):
        if os.path.exists(TEMPDIR):
            shutil.rmtree(TEMPDIR)
        os.mkdir(TEMPDIR)
        os.mkdir(WS_DIR)
        self.dws=find_exe("dws", "Make sure you have enabled your python virtual environment")

    def tearDown(self):
        if os.path.exists(TEMPDIR):
            shutil.rmtree(TEMPDIR)

    def _setup_initial_repo(self, git_resources=None, api_resources=None, hostname=None):
        args = ['init']
        if hostname is not None:
            args.extend(['--hostname', hostname])
        if git_resources is not None:
            args.append('--create-resources='+git_resources)
        self._run_dws(args, cwd=WS_DIR)
        if api_resources is not None:
            for rname in api_resources.split(','):
                self._add_api_resource(rname, cwd=WS_DIR)
        self._run_dws(['status'])


def write_gzipped_json(json_data, filepath):
    data = json.dumps(json_data).encode('utf-8')
    compressed = gzip.compress(data)
    with open(filepath, 'wb') as f:
        f.write(compressed)


def get_configuration_for_test(test_section_name, required_properties=None):
    """Retrieve the configuration section for the
    specify test from test_params.cfg. If the config
    file is not present or the section not present,
    return None.

    If required_properties is specified, it should be a list of property names.
    If they are not all present, a warning will be emitted and None returned.
    """
    config = configparser.ConfigParser()
    files_read = config.read('test_params.cfg')
    if len(files_read)!=1:
        return None
    if config.has_section(test_section_name):
        section = config[test_section_name]
        if required_properties is not None:
            missing_props = [
                prop for prop in required_properties
                if prop not in section
            ]
            if len(missing_props)>0:
                print(f"WARNING: test_params.cfg section '{test_section_name}' missing required properties {', '.join(missing_props)}",
                      file=sys.stderr)
                return None
        return section
    else:
        return None

