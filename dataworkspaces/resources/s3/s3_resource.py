# Copyright 2018,2019 by MPI-SWS and Data-ken Research. Licensed under Apache 2.0. See LICENSE.txt.
"""
Resource for files living in a local directory 
"""
import os
from os.path import join, exists
from typing import Pattern, Tuple, Optional, Set, Union

from s3fs import S3FileSystem # type: ignore


from dataworkspaces.errors import ConfigurationError, NotSupportedError, InternalError
from dataworkspaces.workspace import (
    Workspace,
    Resource,
    ResourceRoles,
    LocalStateResourceMixin,
    FileResourceMixin,
    SnapshotResourceMixin,
    JSONDict,
    JSONList,
    ResourceFactory,
)

from dataworkspaces.utils.param_utils import StringType

from dataworkspaces.resources.s3.snapfs import S3Snapshot
from dataworkspaces.resources.s3.snapshot import snapshot_multiprocess

S3_RESOURCE_TYPE = "s3"


# For anything specific to results resources, we throw this error
RESULTS_ROLE_NOT_SUPPORTED=NotSupportedError(f"{ResourceRoles.RESULTS} not currently supported for S3 resources")


class S3Resource(
    Resource, LocalStateResourceMixin, FileResourceMixin, SnapshotResourceMixin
):
    """Resource class for S3 bucket."""

    def __init__(
        self,
        resource_type: str,
        name: str,
        role: str,
        workspace: Workspace,
        bucket_name: str,
        #region: str,
    ):
        if role==ResourceRoles.RESULTS:
            raise RESULTS_ROLE_NOT_SUPPORTED
        super().__init__(resource_type, name, role, workspace)
        self.param_defs.define(
            "bucket_name",
            default_value=None,
            optional=False,
            is_global=True,
            help="Name of the bucket",
            ptype=StringType(),
        )
        self.bucket_name = self.param_defs.get(
            "bucket_name", bucket_name
        )  # type: str
        # self.param_defs.define(
        #     "region",
        #     default_value=None,
        #     optional=False,
        #     is_global=False,
        #     help="S3 Region for bucket",
        #     ptype=StringType(),
        # )
        # self.region = self.param_defs.get(
        #     "region", region
        # ) # type: str
        # self.fs = S3FileSystem(region=region)
        self.fs = S3FileSystem()

        # local scratch directory is for stuff we keep locally reflecting the
        # current state of our local workspace.
        self.local_scratch_dir = join(join(workspace.get_scratch_directory(), S3_RESOURCE_TYPE),
                                      name)
        # ensure the scratch directory exits
        if not exists(self.local_scratch_dir):
            os.makedirs(self.local_scratch_dir)
        self.current_snapshot_file = join(self.local_scratch_dir, 'current_snapshot.txt')
        self.current_snapshot = None # type: Optional[str]
        self.snapshot_fs = None # type: Optional[S3Snapshot]
        if exists(self.current_snapshot_file):
            with open(self.current_snapshot_file, 'r') as f:
                self.current_snapshot = f.read().strip()
            self.snaphot_fs = self._load_snapshot(self.current_snapshot)
        # we cache snapshot files in a subdirectory of the scratch dir
        self.snapshot_cache_dir = join(self.local_scratch_dir, "snapshot_cache")
        # Make sure it exists.
        if not exists(self.snapshot_cache_dir):
            os.makedirs(self.snapshot_cache_dir)

    def _load_snapshot(self, snapshot_hash:str) -> S3Snapshot:
        snapshot_file = snapshot_hash+'.json.gz'
        snapshot_local_path = join(self.snapshot_cache_dir, snapshot_file)
        if not exists(snapshot_local_path):
            snapshot_s3_path = join(join(self.bucket_name, '.snapshots'),
                                    snapshot_file)
            if not self.fs.exists(snapshot_s3_path):
                raise InternalError(f"File s3://{snapshot_s3_path} not found for snapshot {snapshot_hash}")
            self.fs.get(snapshot_s3_path, snapshot_local_path)
        return S3Snapshot.read_snapshot_from_file(snapshot_file)

    def get_local_path_if_any(self):
        return None

    def results_move_current_files(
        self, rel_dest_root: str, exclude_files: Set[str], exclude_dirs_re: Pattern
    ) -> None:
        raise RESULTS_ROLE_NOT_SUPPORTED

    def results_copy_current_files(
        self, rel_dest_root: str, exclude_files: Set[str], exclude_dirs_re: Pattern
    ) -> None:
        raise RESULTS_ROLE_NOT_SUPPORTED

    def add_results_file(self, data: Union[JSONDict, JSONList], rel_dest_path: str) -> None:
        """save JSON results data to the specified path in the resource.
        """
        raise RESULTS_ROLE_NOT_SUPPORTED

    def _verify_no_snapshot(self):
        if self.current_snapshot is not None:
            raise NotSupportedError("Cannot update bucket when a snapshot is selected. "+
                                    f"Current snapshot is {self.current_snapshot}")

    def upload_file(self, local_path: str, rel_dest_path: str) -> None:
        """Copy a local file to the specified path in the
        resource. This may be a local copy or an upload, depending
        on the resource implmentation
        """
        self._verify_no_snapshot()
        if not exists(local_path):
            raise ConfigurationError("Source file %s does not exist." % local_path)
        self.fs.put(local_path, join(self.bucket_name, rel_dest_path))

    def does_subpath_exist(
        self, subpath: str, must_be_file: bool = False, must_be_directory: bool = False
    ) -> bool:
        if self.current_snapshot:
            assert self.snapshot_fs
            if not self.snapshot_fs.exists(subpath):
                return False
            elif must_be_file:
                return self.snapshot_fs.isfile(subpath)
            elif must_be_directory:
                return not self.snapshot_fs.isfile(subpath)
            else:
                return True
        else:
            if not self.fs.exists(subpath):
                return False
            elif must_be_file:
                return self.fs.isfile(subpath)
            elif must_be_directory:
                return not self.fs.isfile(subpath)
            else:
                return True

    def delete_file(self, rel_path: str) -> None:
        self._verify_no_snapshot()
        self.fs.rm(join(self.bucket_name, rel_path))

    def read_results_file(self, subpath: str) -> JSONDict:
        """Read and parse json results data from the specified path
        in the resource. If the path does not exist or is not a file
        throw a ConfigurationError.
        """
        raise RESULTS_ROLE_NOT_SUPPORTED

    def get_local_params(self) -> JSONDict:
        return {}

    def pull_precheck(self) -> None:
        """Nothing to do, since we donot support sync.
        """
        pass

    def pull(self) -> None:
        """Nothing to do, since we donot support sync.
        """
        pass

    def push_precheck(self) -> None:
        """Nothing to do, since we donot support sync.
        """
        pass

    def push(self) -> None:
        """Nothing to do, since we donot support sync.
        """
        pass

    def snapshot_precheck(self) -> None:
        pass

    def snapshot(self) -> Tuple[Optional[str], Optional[str]]:
        if self.current_snapshot is not None:
            # if a snapshot is already enabled, just return that one
            return (self.current_snapshot, self.current_snapshot)
        else:
            self.current_snapshot, versions = snapshot_multiprocess(self.bucket_name, self.snapshot_cache_dir)
            with open(self.current_snapshot_file, 'w') as f:
                f.write(self.current_snapshot)
            self.snapshot_fs = S3Snapshot(versions)
            return (self.current_snapshot, self.current_snapshot)

    def restore_precheck(self, hashval):
        snapshot_file = hashval+'.json.gz'
        snapshot_local_path = join(self.snapshot_cache_dir, snapshot_file)
        if not exists(snapshot_local_path):
            snapshot_s3_path = join(join(self.bucket_name, '.snapshots'),
                                    snapshot_file)
            if not self.fs.exists(snapshot_s3_path):
                raise ConfigurationError(f"File s3://{snapshot_s3_path} not found for snapshot {hashval}")

    def restore(self, hashval):
        self.snapshot_fs = self._load_snapshot(hashval)
        self.current_snapshot = hashval


    def delete_snapshot(
        self, workspace_snapshot_hash: str, resource_restore_hash: str, relative_path: str
    ) -> None:
        snapshot_file = resource_restore_hash+'.json.gz'
        snapshot_local_path = join(self.snapshot_cache_dir, snapshot_file)
        if exists(snapshot_local_path):
            os.remove(snapshot_local_path)
        snapshot_s3_path = join(join(self.bucket_name, '.snapshots'),
                                snapshot_file)
        if  self.fs.exists(snapshot_s3_path):
            self.fs.remove(snapshot_s3_path)


    def validate_subpath_exists(self, subpath: str) -> None:
        if self.current_snapshot is not None:
            assert self.snapshot_fs is not None
            if not self.snapshot_fs.exists(subpath):
                raise ConfigurationError(f"Subpath {subpath} does not existing in bucket {self.bucket_name} as of snapshot {self.current_snapshot}")
        elif not self.fs.exists(subpath):
            raise ConfigurationError(f"Subpath {subpath} does not currently exist in bucket {self.bucket_name}")

    def __str__(self):
        return "S3 Resource bucket %s in role '%s'" % (self.bucket_name, self.role)



class S3ResourceFactory(ResourceFactory):
    def from_command_line(self, role, name, workspace, bucket_name):
        """Instantiate a resource object from the add command's arguments"""
        return S3Resource(
            S3_RESOURCE_TYPE,
            name,
            role,
            workspace,
            bucket_name
        )

    def from_json(
        self, params: JSONDict, local_params: JSONDict, workspace: Workspace
    ) -> S3Resource:
        """Instantiate a resource object from saved params and local params"""
        return S3Resource(
            S3_RESOURCE_TYPE,
            params["name"],
            params["role"],
            workspace,
            params['bucket_name'])

    def has_local_state(self) -> bool:
        return False

    def clone(self, params: JSONDict, workspace: Workspace) -> LocalStateResourceMixin:
        """Instantiate a resource that was created remotely. This should not be called,
        since we have no local state.
        """
        raise InternalError("Clone called for S3 resource {params['name']}")

    def suggest_name(self, workspace, role, bucket_name):
        return bucket_name
