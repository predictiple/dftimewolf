# -*- coding: utf-8 -*-
"""Processes GCP cloud disks using Turbinia."""

from __future__ import unicode_literals
from __future__ import absolute_import
from __future__ import print_function

import getpass
import os
import tempfile

from turbinia import client as turbinia_client
from turbinia import config as turbinia_config
from turbinia import evidence
from turbinia import output_manager
from turbinia import TurbiniaException
from turbinia.message import TurbiniaRequest

from dftimewolf.lib import module
from dftimewolf.lib.containers import containers
from dftimewolf.lib.modules import manager as modules_manager

# pylint: disable=no-member


class TurbiniaProcessor(module.BaseModule):
  """Processes Google Cloud (GCP) disks with Turbinia.

  Attributes:
    client (TurbiniaClient): Turbinia client.
    disk_name (str): name of the disk to process.
    instance (str): name of the Turbinia instance
    project (str): name of the GPC project containing the disk to process.
    turbinia_region (str): GCP region in which the Turbinia server is running.
    turbinia_zone (str): GCP zone in which the Turbinia server is running.
  """

  def __init__(self, state, critical=False):
    """Initializes a Turbinia Google Cloud (GCP) disks processor.

    Args:
      state (DFTimewolfState): recipe state.
      critical (Optional[bool]): True if the module is critical, which causes
          the entire recipe to fail if the module encounters an error.
    """
    super(TurbiniaProcessor, self).__init__(state, critical=critical)
    self._output_path = None
    self.client = None
    self.disk_name = None
    self.instance = None
    self.project = None
    self.turbinia_region = None
    self.turbinia_zone = None
    self.sketch_id = None
    self.run_all_jobs = None

  # pylint: disable=arguments-differ
  def SetUp(self, disk_name, project, turbinia_zone, sketch_id, run_all_jobs):
    """Sets up the object attributes.

    Args:
      disk_name (str): name of the disk to process.
      project (str): name of the GPC project containing the disk to process.
      turbinia_zone (str): GCP zone in which the Turbinia server is running.
      sketch_id (int): The Timesketch sketch id
      run_all_jobs (bool): Whether to run all jobs instead of a faster subset.
    """
    # TODO: Consider the case when multiple disks are provided by the previous
    # module or by the CLI.

    if project is None or turbinia_zone is None:
      self.state.AddError(
          'project or turbinia_zone are not all specified, bailing out',
          critical=True)
      return

    self.disk_name = disk_name
    self.project = project
    self.turbinia_zone = turbinia_zone
    self.sketch_id = sketch_id
    self.run_all_jobs = run_all_jobs

    try:
      turbinia_config.LoadConfig()
      self.turbinia_region = turbinia_config.TURBINIA_REGION
      self.instance = turbinia_config.PUBSUB_TOPIC
      if turbinia_config.TURBINIA_PROJECT != self.project:
        self.state.AddError(
            'Specified project {0:s} does not match Turbinia configured '
            'project {1:s}. Use gcp_turbinia_import recipe to copy the disk '
            'into the same project.'.format(
                self.project, turbinia_config.TURBINIA_PROJECT), critical=True)
        return
      self._output_path = tempfile.mkdtemp()
      self.client = turbinia_client.TurbiniaClient()
    except TurbiniaException as exception:
      # TODO: determine if exception should be converted into a string as
      # elsewhere in the codebase.
      self.state.AddError(exception, critical=True)
      return

  def Process(self):
    """Process files with Turbinia."""
    log_file_path = os.path.join(self._output_path, 'turbinia.log')
    print('Turbinia log file: {0:s}'.format(log_file_path))

    if self.state.input and not self.disk_name:
      _, disk = self.state.input[0]
      self.disk_name = disk.name
      print('Using disk {0:s} from previous collector'.format(self.disk_name))

    evidence_ = evidence.GoogleCloudDisk(
        disk_name=self.disk_name, project=self.project, zone=self.turbinia_zone)
    try:
      evidence_.validate()
    except TurbiniaException as exception:
      self.state.AddError(exception, critical=True)
      return

    request = TurbiniaRequest(requester=getpass.getuser())
    request.evidence.append(evidence_)
    if self.sketch_id:
      request.recipe['sketch_id'] = self.sketch_id
    if not self.run_all_jobs:
      request.recipe['jobs_blacklist'] = ['StringsJob']

    # Get threat intelligence data from any modules that have stored some.
    # In this case, observables is a list of containers.ThreatIntelligence
    # objects.
    threatintel = self.state.GetContainers(containers.ThreatIntelligence)
    if threatintel:
      print('Sending {0:d} threatintel to Turbinia GrepWorkers...'.format(
          len(threatintel)))
      indicators = [item.indicator for item in threatintel]
      request.recipe['filter_patterns'] = indicators

    request_dict = {
        'instance': self.instance,
        'project': self.project,
        'region': self.turbinia_region,
        'request_id': request.request_id
    }

    try:
      print('Creating Turbinia request {0:s} with Evidence {1!s}'.format(
          request.request_id, evidence_.name))
      self.client.send_request(request)
      print('Waiting for Turbinia request {0:s} to complete'.format(
          request.request_id))
      self.client.wait_for_request(**request_dict)
      task_data = self.client.get_task_data(**request_dict)
    except TurbiniaException as exception:
      # TODO: determine if exception should be converted into a string as
      # elsewhere in the codebase.
      self.state.AddError(exception, critical=True)
      return

    message = self.client.format_task_status(**request_dict, full_report=True)
    short_message = self.client.format_task_status(**request_dict)
    print(short_message)

    # Store the message for consumption by any reporting modules.
    report = containers.Report(
        module_name='TurbiniaProcessor', text=message, text_format='markdown')
    self.state.StoreContainer(report)

    # This finds all .plaso files in the Turbinia output, and determines if they
    # are local or remote (it's possible this will be running against a local
    # instance of Turbinia).
    local_paths = []
    gs_paths = []
    timeline_label = '{0:s}-{1:s}'.format(self.project, self.disk_name)
    for task in task_data:
      # saved_paths may be set to None
      for path in task.get('saved_paths') or []:
        if path.startswith('/') and path.endswith('.plaso'):
          local_paths.append(path)
        if path.startswith('gs://') and path.endswith('.plaso'):
          gs_paths.append(path)

    if not local_paths and not gs_paths:
      self.state.AddError(
          'No .plaso files found in Turbinia output.', critical=True)
      return

    # Any local .plaso files that exist we can add immediately to the output
    self.state.output = [
        (timeline_label, p) for p in local_paths if os.path.exists(p)]

    # For files remote in GCS we copy each plaso file back from GCS and then add
    # to output paths
    # TODO: Externalize fetching files from GCS buckets to a different module.
    for path in gs_paths:
      local_path = None
      try:
        output_writer = output_manager.GCSOutputWriter(
            path, local_output_dir=self._output_path)
        local_path = output_writer.copy_from(path)
      except TurbiniaException as exception:
        # TODO: determine if exception should be converted into a string as
        # elsewhere in the codebase.
        self.state.AddError(exception, critical=True)
        return

      if local_path:
        self.state.output.append((timeline_label, local_path))

    if not self.state.output:
      self.state.AddError('No .plaso files could be found.', critical=True)


modules_manager.ModulesManager.RegisterModule(TurbiniaProcessor)
