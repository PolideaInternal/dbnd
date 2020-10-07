import logging
import os

from copy import deepcopy
from typing import Type

from dbnd._core.parameter.parameter_definition import (
    ParameterDefinition,
    _ParameterKind,
)
from dbnd._core.task_run.task_run_ctrl import TaskRunCtrl
from targets import DbndLocalFileMetadataRegistry, FileTarget, target
from targets.multi_target import MultiTarget


logger = logging.getLogger(__name__)


class TaskRunLocalSyncer(TaskRunCtrl):
    """
    This ctrl is in charge of syncing all required files/folders locally for execution (direct read/write)
    """

    def __init__(self, task_run):
        super(TaskRunLocalSyncer, self).__init__(task_run=task_run)
        self.inputs_to_sync = []
        self.outputs_to_sync = []
        self.local_sync_root = self.task_env.dbnd_local_root.partition("local_sync")

        for p_def, p_val in self.task._params.get_param_values(user_only=True):
            if (
                isinstance(p_val, FileTarget)
                and p_val.config.require_local_access
                and not p_val.fs.local
            ):
                # Target requires local access, it points to a remote path that must be synced-to from a local path
                local_target = self._local_cache_target(p_val)

                self._sync_input_or_output(p_def, p_val, local_target)

            elif isinstance(p_val, MultiTarget):
                local_targets = []
                for target_ in p_val.targets:
                    if target_.config.require_local_access and not target_.fs.local:
                        local_target = self._local_cache_target(target_)
                        self._sync_input_or_output(p_def, target_, local_target)
                        local_target.append(local_target)

                if local_targets:
                    local_multitarget = MultiTarget(local_targets)

    def _local_cache_target(self, target_):
        # type: (FileTarget) -> FileTarget
        return target(
            self.task_run.attemp_folder_local_cache,
            os.path.basename(target_.path),
            config=target_.config,
        )

    def _sync_input_or_output(self, param_definition, old_target, new_target):
        # type: (ParameterDefinition, Type[DataTarget], Type[DataTarget]) -> None
        if param_definition.kind == _ParameterKind.task_output:
            # Output should be substituted for local path and synced post execution
            self.outputs_to_sync.append((param_definition, old_target, new_target))

        else:
            # Use DbndLocalFileMetadataRegistry to sync inputs only when necessary
            dbnd_meta_cache = DbndLocalFileMetadataRegistry.get_or_create(new_target)

            # When syncing remote to local -> We can't use MD5, so use TTL instead
            if dbnd_meta_cache.expired or not new_target.exists():
                # If TTL is invalid, or local file doesn't exist -> Sync to local
                self.inputs_to_sync.append((param_definition, old_target, new_target))

    def sync_pre_execute(self):
        if self.inputs_to_sync:
            for p_def, p_val, local_target in self.inputs_to_sync:
                # Input should be synced to local path and substituted
                try:
                    logger.info("Downloading  %s %s to %s", p_def, p_val, local_target)
                    local_target.mkdir_parent()
                    p_val.download(
                        local_target.path,
                        overwrite=local_target.config.overwrite_target,
                    )
                except Exception as ex:
                    logger.exception(
                        "Failed to create local cache for %s %s at %s",
                        p_def,
                        p_val,
                        local_target,
                    )
                    raise
                setattr(self.task, p_def.name, local_target)
            logger.info(
                "All required task inputs are downloaded to %s",
                self.task_run.attemp_folder_local_cache,
            )

        for p_def, p_val, local_target in self.outputs_to_sync:
            local_target.mkdir_parent()
            # Output should be substituted for local path and synced post execution
            setattr(self.task, p_def.name, local_target)

    def sync_post_execute(self):
        for p_def, p_val, local_target in self.inputs_to_sync:
            setattr(self.task, p_def.name, p_val)

        for p_def, p_val, local_target in self.outputs_to_sync:
            try:
                logger.info("Uploading  %s %s from %s", p_def, p_val, local_target)
                p_val.copy_from_local(local_path=local_target.path)
                if p_val.config.flag:
                    p_val.mark_success()
            except Exception as ex:
                logger.exception(
                    "Failed to upload task output %s %s from %s",
                    p_def,
                    p_val,
                    local_target,
                )
                raise
            setattr(self.task, p_def.name, p_val)
