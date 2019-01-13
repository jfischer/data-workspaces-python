# Copyright 2018,2019 by MPI-SWS and Data-ken Research. Licensed under Apache 2.0. See LICENSE.txt.
from setuptools import setup

import sys
sys.path.insert(0, 'dataworkspaces')
from dataworkspaces import __version__

setup(
    name='Data Workspaces',
    version=__version__,
    py_modules=['dataworkspaces'],
    install_requires=[
        'Click',
    ],
    entry_points="""
        [console_scripts]
        dws=dataworkspaces.__main__
    """
)
