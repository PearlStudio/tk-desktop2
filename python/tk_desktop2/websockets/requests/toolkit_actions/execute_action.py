# Copyright 2018 Autodesk, Inc.  All rights reserved.
#
# Use of this software is subject to the terms of the Autodesk license agreement
# provided at the time of installation or download, or which otherwise accompanies
# this software in either electronic or hard copy form.
#

import sgtk
import threading
from ..request import WebsocketsRequest

logger = sgtk.LogManager.get_logger(__name__)

external_config = sgtk.platform.import_framework(
    "tk-framework-shotgunutils",
    "external_config"
)


class ExecuteActionWebsocketsRequest(WebsocketsRequest):
    """
    Executes the given toolkit action.

    There are several ways this is sent from Shotgun.
    The command supports the following:

    From the normal spreadsheet views:

        { 'entity_ids': [6947],
          'entity_type': 'Version',
          'name': 'Jump to Screening Room in RV',
          'pc': 'Primary',
          'pc_root_path': '',
          'project_id': 87,
          'title': 'Jump to Screening Room in RV',
          'user': {...},
          'name': 'execute_action'
        }

    From my tasks - note how project id is None and
    the entity_ids syntax is different:

        { 'entity_ids': [{'id': 5757, 'type': 'Task'}],
          'entity_type': 'Task',
          'name': 'nuke_9.0v6',
          'pc': 'Primary',
          'pc_root_path': '',
          'project_id': None,
          'title': 'Nuke 9.0v6',
          'user': {...},
          'name': 'execute_action'
        }

    Expected response::

        Standard format as generated by WebsocketsRequest._reply_with_status()

    """

    def __init__(self, connection, id, parameters):
        """
        :param connection: Associated :class:`WebsocketsConnection`.
        :param int id: Id for this request.
        :param dict parameters: Command parameters (see syntax above)
        :raises: ValueError
        """
        super(ExecuteActionWebsocketsRequest, self).__init__(connection, id)

        # note - parameter data is coming in from javascript so we
        #        perform some in-depth validation of the values
        #        prior to blindly accepting them.
        required_params = [
            "name",
            "title",
            "pc",
            "entity_ids",
            "entity_type",
            "project_id"
        ]
        for required_param in required_params:
            if required_param not in parameters:
                raise ValueError("%s: Missing parameter '%s' in payload." % (self, required_param))

        self._resolved_command = None
        self._command_name = parameters["name"]
        self._command_title = parameters["title"]
        self._config_name = parameters["pc"]
        self._entity_type = parameters["entity_type"]

        first_entity_object = parameters["entity_ids"][0]

        # now determine if the entity_ids holds a list of ids or a
        # list of dictionaries (see protocol summary above for details)
        if isinstance(first_entity_object, dict):
            # it's a std entity dict
            self._entity_id = first_entity_object["id"]
            # Support for commands that support running on multiple entities at
            # once. We'll keep track of a list of all entity ids that were passed
            # to us.
            self._entity_ids = [e["id"] for e in parameters["entity_ids"]]
        else:
            # it's just the id
            self._entity_id = first_entity_object
            # Legacy support for commands that support running on multiple entities
            # at once. We'll keep track of a list of all entity ids that were passed
            # to us.
            self._entity_ids = parameters["entity_ids"]

        # now determine if we need to resolve the project id
        if parameters.get("project_id") is None:
            # resolve project id in case we are on a non-project page
            # todo: this could be handled in a far more elegant way on the javascript side
            sg_data = connection.shotgun.find_one(
                self._entity_type,
                [["id", "is", self._entity_id]],
                ["project"]
            )
            self._project_id = sg_data["project"]["id"]
        else:
            # for project pages, project_id is passed down.
            self._project_id = parameters["project_id"]

    @property
    def requires_toolkit(self):
        """
        True if the request requires toolkit
        """
        return True

    @property
    def project_id(self):
        """
        Project id associated with this request
        """
        return self._project_id

    @property
    def entity_type(self):
        """
        Entity type associated with this request
        """
        return self._entity_type

    @property
    def entity_id(self):
        """
        Entity id associated with this request
        """
        return self._entity_id

    def _execute(self):
        """
        Thread execution payload
        """
        try:
            if self._resolved_command.support_shotgun_multiple_selection:
                output = self._resolved_command.execute_on_multiple_entities(
                    pre_cache=True,
                    entity_ids=self._entity_ids,
                )
            else:
                output = self._resolved_command.execute(pre_cache=True)
            self._reply_with_status(output=output)
        except Exception as e:

            logger.debug("Could not execute action", exc_info=True)

            # handle the special case where we are calling an older version of the Shotgun
            # engine which doesn't support PySide2 (v0.7.0 or earlier). In this case, trap the
            # error message sent from the engine and replace it with a more specific one:
            #
            # The error message from the engine looks like this:
            # Looks like you are trying to run a Sgtk App that uses a QT based UI,
            # however the Shotgun engine could not find a PyQt or PySide installation in
            # your python system path. We recommend that you install PySide if you want to
            # run UI applications from within Shotgun.

            if "Looks like you are trying to run a Sgtk App that uses a QT based UI" in str(e):
                self._reply_with_status(
                    status=1,
                    error=(
                        "The version of the Toolkit Shotgun Engine (tk-shotgun) you "
                        "are running does not support PySide2. Please upgrade your "
                        "configuration to use version v0.8.0 or above of the engine."
                    )
                )

            else:
                # bubble up the error message
                self._reply_with_status(
                    status=1,
                    error=str(e)
                )

    def execute_with_context(self, associated_commands):
        """
        Executes the request async.

        Passes a fully loaded external
        configuration state to aid execution, laid out in the following
        structure:

        [
            {
                "configuration": <ExternalConfiguration>,
                "commands": [<ExternalCommand>, ...],
                "error": None
            },
            {
                "configuration": <ExternalConfiguration>,
                "commands": None,
                "error": "Something went wrong"
            },
        ]

        :param list associated_commands: See above for details.
        :raises: RuntimeError
        """
        # locate the requested command in our configuration
        for config in associated_commands:

            # this is a zero config setup with no record in Shotgun
            # such a config is expected to be named Primary in Shotgun
            config_name = config["configuration"].pipeline_configuration_name or "Primary"

            if config_name == self._config_name:
                for command in config["commands"] or []:
                    if command.system_name == self._command_name:
                        self._resolved_command = command
                        break

        if not self._resolved_command:
            raise RuntimeError("%s: Configuration mismatch!" % self)

        # execute external command in a thread to not block
        worker = threading.Thread(target=self._execute)
        # if the python environment shuts down, no need to wait for this thread
        worker.daemon = True
        # launch external process
        worker.start()
