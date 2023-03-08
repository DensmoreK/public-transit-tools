############################################################################
## Tool name: Transit Network Analysis Tools
## Created by: Melinda Morang, Esri
## Last updated: 3 May 2022
############################################################################
"""Count the number of destinations reachable from each origin by transit and
walking. The tool calculates an Origin-Destination Cost Matrix for each start
time within a time window because the reachable destinations change depending
on the time of day because of the transit schedules.  The output gives the
total number of destinations reachable at least once as well as the number of
destinations reachable at least 10%, 20%, ...90% of start times during the time
window.  The number of reachable destinations can be weighted based on a field,
such as the number of jobs available at each destination.  The tool also
calculates the percentage of total destinations reachable.

This script should be launched by the CalculateODMatrixInParallel.py
script as a subprocess. It computes the OD Cost Matrix in parallel for all time
increments, chunking the origins and destinations if necessary, and calculates
the desired statistics on the outputs.

This version of the tool is for ArcGIS Pro only and solves the OD Cost Matrices in
parallel. It was built based off Esri's Solve Large OD Cost Matrix sample script
available from https://github.com/Esri/large-network-analysis-tools under an Apache
2.0 license.

Copyright 2022 Esri
   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at
       http://www.apache.org/licenses/LICENSE-2.0
   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""
# pylint: disable=logging-fstring-interpolation
from concurrent import futures
import os
import sys
import uuid
import logging
import shutil
import itertools
import time
import traceback
import argparse
import pandas as pd
from functools import partial

import arcpy

# Commenting out the Arrow stuff because it doesn't gain us much and makes the code complicated.
# Retaining it here in case we want to reimplement it later
# # The OD Cost Matrix toArrowTable() method was added in ArcGIS Pro 2.9. Writing intermediate OD outputs to Arrow
# # tables is more space and memory efficient than writing CSV files, so prefer this method when possible. If the
# # ArcGIS Pro version is < 2.9, though, fall back to using CSV files.
# if arcpy.GetInstallInfo()["Version"] < "2.9":
#     USE_ARROW = False
#     import csv
# else:
#     USE_ARROW = True
#     import pyarrow as pa
USE_ARROW = False
import csv

# Import OD Cost Matrix settings from config file
import CalculateAccessibilityMatrix_OD_config
import CalculateTravelTimeStatistics_OD_config
import AnalysisHelpers

arcpy.env.overwriteOutput = True

# Set logging for the main process.
# LOGGER logs everything from the main process to stdout using a specific format that the SolveLargeODCostMatrix tool
# can parse and write to the geoprocessing message feed.
LOG_LEVEL = logging.INFO  # Set to logging.DEBUG to see verbose debug messages
LOGGER = logging.getLogger(__name__)  # pylint:disable=invalid-name
LOGGER.setLevel(LOG_LEVEL)
sys.stdout.reconfigure(encoding="utf-8")
console_handler = logging.StreamHandler(stream=sys.stdout)
console_handler.setLevel(LOG_LEVEL)
# Used by script tool to split message text from message level to add correct message type to GP window
console_handler.setFormatter(logging.Formatter("%(levelname)s" + AnalysisHelpers.MSG_STR_SPLITTER + "%(message)s"))
LOGGER.addHandler(console_handler)

DELETE_INTERMEDIATE_OD_OUTPUTS = True  # Set to False for debugging purposes


def run_gp_tool(tool, tool_args=None, tool_kwargs=None, log_to_use=LOGGER):
    """Run a geoprocessing tool with nice logging.

    The purpose of this function is simply to wrap the call to a geoprocessing tool in a way that we can log errors,
    warnings, and info messages as well as tool run time into our logging. This helps pipe the messages back to our
    script tool dialog.

    Args:
        tool (arcpy geoprocessing tool class): GP tool class command, like arcpy.management.CreateFileGDB
        tool_args (list, optional): Ordered list of values to use as tool arguments. Defaults to None.
        tool_kwargs (dictionary, optional): Dictionary of tool parameter names and values that can be used as named
            arguments in the tool command. Defaults to None.
        log_to_use (logging.logger, optional): logger class to use for messages. Defaults to LOGGER. When calling this
            from the ODCostMatrix class, use self.logger instead so the messages go to the processes's log file instead
            of stdout.

    Returns:
        GP result object: GP result object returned from the tool run.

    Raises:
        arcpy.ExecuteError if the tool fails
    """
    # Try to retrieve and log the name of the tool
    tool_name = repr(tool)
    try:
        tool_name = tool.__esri_toolname__
    except Exception:  # pylint: disable=broad-except
        try:
            tool_name = tool.__name__
        except Exception:  # pylint: disable=broad-except
            # Probably the tool didn't have an __esri_toolname__ property or __name__. Just don't worry about it.
            pass
    log_to_use.debug(f"Running geoprocessing tool {tool_name}...")

    # Try running the tool, and log all messages
    try:
        if tool_args is None:
            tool_args = []
        if tool_kwargs is None:
            tool_kwargs = {}
        result = tool(*tool_args, **tool_kwargs)
        info_msgs = [msg for msg in result.getMessages(0).splitlines() if msg]
        warning_msgs = [msg for msg in result.getMessages(1).splitlines() if msg]
        for msg in info_msgs:
            log_to_use.debug(msg)
        for msg in warning_msgs:
            log_to_use.warning(msg)
    except arcpy.ExecuteError:
        log_to_use.error(f"Error running geoprocessing tool {tool_name}.")
        # First check if it's a tool error and if so, handle warning and error messages.
        info_msgs = [msg for msg in arcpy.GetMessages(0).strip("\n").splitlines() if msg]
        warning_msgs = [msg for msg in arcpy.GetMessages(1).strip("\n").splitlines() if msg]
        error_msgs = [msg for msg in arcpy.GetMessages(2).strip("\n").splitlines() if msg]
        for msg in info_msgs:
            log_to_use.debug(msg)
        for msg in warning_msgs:
            log_to_use.warning(msg)
        for msg in error_msgs:
            log_to_use.error(msg)
        raise
    except Exception:
        # Unknown non-tool error
        log_to_use.error(f"Error running geoprocessing tool {tool_name}.")
        errs = traceback.format_exc().splitlines()
        for err in errs:
            log_to_use.error(err)
        raise

    log_to_use.debug(f"Finished running geoprocessing tool {tool_name}.")
    return result


class ODCostMatrix:  # pylint:disable = too-many-instance-attributes
    """Used for solving an OD Cost Matrix problem in parallel for a designated chunk of the input datasets."""

    def __init__(self, **kwargs):
        """Initialize the OD Cost Matrix analysis for the given inputs.

        Expected arguments:
        - tool
        - origins
        - destinations
        - destination_where_clause
        - network_data_source
        - travel_mode
        - time_units
        - cutoff
        - scratch_folder
        - od_output_location
        - barriers
        """
        self.tool = kwargs["tool"]
        self.origins = kwargs["origins"]
        self.destinations = kwargs["destinations"]
        self.destination_where_clause = kwargs["destination_where_clause"]
        self.network_data_source = kwargs["network_data_source"]
        self.travel_mode = kwargs["travel_mode"]
        self.time_units = None
        self.cutoff = None
        if self.tool is AnalysisHelpers.ODTool.CalculateAccessibilityMatrix:
            self.time_units = kwargs["time_units"]
            self.cutoff = kwargs["cutoff"]
        self.scratch_folder = kwargs["scratch_folder"]
        self.od_output_location = kwargs["od_output_location"]
        self.barriers = []
        if "barriers" in kwargs:
            self.barriers = kwargs["barriers"]

        if self.tool is AnalysisHelpers.ODTool.CalculateAccessibilityMatrix:
            self.output_fields = ["OriginOID", "DestinationOID"]
        elif self.tool is AnalysisHelpers.ODTool.CalculateTravelTimeStatistics:
            self.output_fields = ["OriginOID", "DestinationOID", "Total_Time"]
        else:
            raise ValueError("Invalid OD Cost Matrix tool.")

        # Create a job ID and a folder for this job
        self.job_id = uuid.uuid4().hex

        # Setup the class logger. Logs for each parallel process are not written to the console but instead to a
        # process-specific log file.
        self.log_file = os.path.join(self.scratch_folder, f"ODCostMatrix_{self.job_id}.log")
        cls_logger = logging.getLogger("ODCostMatrix_" + self.job_id)
        self.setup_logger(cls_logger)
        self.logger = cls_logger

        # Set up other instance attributes
        self.is_service = AnalysisHelpers.is_nds_service(self.network_data_source)
        self.od_solver = None
        self.input_origins_layer = "InputOrigins" + self.job_id
        self.input_destinations_layer = "InputDestinations" + self.job_id
        self.input_origins_layer_obj = None
        self.input_destinations_layer_obj = None
        self.solve_result = None

        # Create a network dataset layer
        self.nds_layer_name = "NetworkDatasetLayer"
        if not self.is_service:
            self._make_nds_layer()
            self.network_data_source = self.nds_layer_name

        # Prepare a dictionary to store info about the analysis results
        self.job_result = {
            "jobId": self.job_id,
            "solveSucceeded": False,
            "solveMessages": "",
            "outputLines": "",
            "logFile": self.log_file
        }

        # Get the ObjectID fields for origins and destinations
        desc_origins = arcpy.Describe(self.origins)
        desc_destinations = arcpy.Describe(self.destinations)
        self.origins_oid_field_name = desc_origins.oidFieldName
        self.destinations_oid_field_name = desc_destinations.oidFieldName
        self.orig_origin_oid_field = "Orig_Origin_OID"
        self.orig_dest_oid_field = "Orig_Dest_OID"

    def _make_nds_layer(self):
        """Create a network dataset layer if one does not already exist."""
        if self.is_service:
            return
        if arcpy.Exists(self.nds_layer_name):
            self.logger.debug(f"Using existing network dataset layer: {self.nds_layer_name}")
        else:
            self.logger.debug("Creating network dataset layer...")
            run_gp_tool(
                arcpy.na.MakeNetworkDatasetLayer,
                [self.network_data_source, self.nds_layer_name],
                log_to_use=self.logger
            )

    def initialize_od_solver(self, time_of_day=None):
        """Initialize an OD solver object and set properties."""
        # For a local network dataset, we need to checkout the Network Analyst extension license.
        if not self.is_service:
            arcpy.CheckOutExtension("network")

        # Create a new OD cost matrix object
        self.logger.debug("Creating OD Cost Matrix object...")
        self.od_solver = arcpy.nax.OriginDestinationCostMatrix(self.network_data_source)

        # Set the OD cost matrix analysis properties.
        # Read properties from the OD config file for all properties not set in the UI as parameters.
        # OD properties documentation: https://pro.arcgis.com/en/pro-app/arcpy/network-analyst/odcostmatrix.htm
        # The properties have been extracted to the config file to make them easier to find and set so users don't have
        # to dig through the code to change them.
        self.logger.debug("Setting OD Cost Matrix analysis properties from OD config file...")
        if self.tool is AnalysisHelpers.ODTool.CalculateAccessibilityMatrix:
            od_props = CalculateAccessibilityMatrix_OD_config.OD_PROPS
            od_props_set_by_tool = CalculateAccessibilityMatrix_OD_config.OD_PROPS_SET_BY_TOOL
        elif self.tool is AnalysisHelpers.ODTool.CalculateTravelTimeStatistics:
            od_props = CalculateTravelTimeStatistics_OD_config.OD_PROPS
            od_props_set_by_tool = CalculateTravelTimeStatistics_OD_config.OD_PROPS_SET_BY_TOOL
        else:
            raise ValueError("Invalid OD Cost Matrix tool.")
        for prop in od_props:
            if prop in od_props_set_by_tool:
                self.logger.warning(
                    f"OD config file property {prop} is handled explicitly by the tool parameters and will be ignored."
                )
                continue
            try:
                setattr(self.od_solver, prop, od_props[prop])
            except Exception as ex:  # pylint: disable=broad-except
                # Suppress warnings for older services (pre 11.0) that don't support locate settings and services
                # that don't support accumulating attributes because we don't want the tool to always throw a warning.
                if not (self.is_service and prop in [
                    "searchTolerance", "searchToleranceUnits", "accumulateAttributeNames"
                ]):
                    self.logger.warning(
                        f"Failed to set property {prop} from OD config file. Default will be used instead.")
                    self.logger.warning(str(ex))
        # Set properties explicitly specified in the tool UI as arguments
        self.logger.debug("Setting OD Cost Matrix analysis properties specified tool inputs...")
        self.od_solver.travelMode = self.travel_mode
        if self.tool is AnalysisHelpers.ODTool.CalculateAccessibilityMatrix:
            self.od_solver.timeUnits = self.time_units
            self.od_solver.defaultImpedanceCutoff = self.cutoff
        # Set time of day, which is passed in as an OD solve parameter from our chunking mechanism
        self.od_solver.timeOfDay = time_of_day

        # Ensure the travel mode has impedance units that are time-based.
        self._validate_travel_mode()

    def _validate_travel_mode(self):
        """Validate that the travel mode has time units.

        Raises:
            ValueError: If the travel mode's impedance units are not time based.
        """
        # Get the travel mode object from the already-instantiated OD solver object. This saves us from having to parse
        # the user's input travel mode from its string name, object, or json representation.
        travel_mode = self.od_solver.travelMode
        impedance = travel_mode.impedance
        time_attribute = travel_mode.timeAttributeName
        if impedance != time_attribute:
            err = f"The impedance units of the selected travel mode {travel_mode.name} are not time based."
            self.logger.error(err)
            raise ValueError(err)

    def solve(self, origins_criteria, destinations_criteria, time_of_day):
        """Create and solve an OD Cost Matrix analysis for the designated chunk of origins and destinations.

        Args:
            origins_criteria (list): Origin ObjectID range to select from the input dataset
            destinations_criteria ([type]): Destination ObjectID range to select from the input dataset
            time_of_day (datetime): Time of day for this solve
        """
        # Select the origins and destinations to process
        self._select_inputs(origins_criteria, destinations_criteria)

        # Initialize the OD solver object
        self.initialize_od_solver(time_of_day)

        # Load the origins
        self.logger.debug("Loading origins...")
        origins_field_mappings = self.od_solver.fieldMappings(
            arcpy.nax.OriginDestinationCostMatrixInputDataType.Origins,
            True  # Use network location fields
        )
        self.od_solver.load(
            arcpy.nax.OriginDestinationCostMatrixInputDataType.Origins,
            self.input_origins_layer_obj,
            origins_field_mappings,
            False
        )

        # Load the destinations
        self.logger.debug("Loading destinations...")
        destinations_field_mappings = self.od_solver.fieldMappings(
            arcpy.nax.OriginDestinationCostMatrixInputDataType.Destinations,
            True  # Use network location fields
        )
        self.od_solver.load(
            arcpy.nax.OriginDestinationCostMatrixInputDataType.Destinations,
            self.input_destinations_layer_obj,
            destinations_field_mappings,
            False
        )

        # Load barriers
        # Note: This loads ALL barrier features for every analysis, even if they are very far away from any of
        # the inputs in the current chunk. You may want to select only barriers within a reasonable distance of the
        # inputs, particularly if you run into the maximumFeaturesAffectedByLineBarriers,
        # maximumFeaturesAffectedByPointBarriers, and maximumFeaturesAffectedByPolygonBarriers tool limits for portal
        # solves. However, since barriers and portal solves with limits are unusual for this tool, deal with this only
        # if it becomes a problem.
        for barrier_fc in self.barriers:
            self.logger.debug(f"Loading barriers feature class {barrier_fc}...")
            shape_type = arcpy.Describe(barrier_fc).shapeType
            if shape_type == "Polygon":
                class_type = arcpy.nax.OriginDestinationCostMatrixInputDataType.PolygonBarriers
            elif shape_type == "Polyline":
                class_type = arcpy.nax.OriginDestinationCostMatrixInputDataType.LineBarriers
            elif shape_type == "Point":
                class_type = arcpy.nax.OriginDestinationCostMatrixInputDataType.PointBarriers
            else:
                self.logger.warning(
                    f"Barrier feature class {barrier_fc} has an invalid shape type and will be ignored."
                )
                continue
            barriers_field_mappings = self.od_solver.fieldMappings(class_type, True)
            self.od_solver.load(class_type, barrier_fc, barriers_field_mappings, True)

        # Solve the OD cost matrix analysis
        self.logger.debug("Solving OD cost matrix...")
        solve_start = time.time()
        self.solve_result = self.od_solver.solve()
        solve_end = time.time()
        self.logger.debug(f"Solving OD cost matrix completed in {round(solve_end - solve_start, 3)} (seconds).")

        # Handle solve messages
        solve_msgs = [msg[-1] for msg in self.solve_result.solverMessages(arcpy.nax.MessageSeverity.All)]
        initial_num_msgs = len(solve_msgs)
        for msg in solve_msgs:
            self.logger.debug(msg)
        # Remove repetitive messages so they don't clog up the stdout pipeline when running the tool
        # 'No "Destinations" found for "Location 1" in "Origins".' is a common message that tends to be repeated and is
        # not particularly useful to see in bulk.
        # Note that this will not work for localized software when this message is translated.
        common_msg_prefix = 'No "Destinations" found for '
        solve_msgs = [msg for msg in solve_msgs if not msg.startswith(common_msg_prefix)]
        num_msgs_removed = initial_num_msgs - len(solve_msgs)
        if num_msgs_removed:
            self.logger.debug(f"Repetitive messages starting with {common_msg_prefix} were consolidated.")
            solve_msgs.append(f"No destinations were found for {num_msgs_removed} origins.")
        solve_msgs = "\n".join(solve_msgs)

        # Update the result dictionary
        self.job_result["solveMessages"] = solve_msgs
        if not self.solve_result.solveSucceeded:
            self.logger.debug("Solve failed.")
            return
        self.logger.debug("Solve succeeded.")
        self.job_result["solveSucceeded"] = True

        # Read the results to discover all destinations reached by the origins in this chunk and store the output
        # in a file with a specific naming scheme.
        # Example: ODLines_O_1_1000_D_2001_3000_T_20220428_091500.csv
        time_string = time_of_day.strftime("%Y%m%d_%H%M%S")
        out_filename = (
            f"ODLines_O_{origins_criteria[0]}_{origins_criteria[1]}_"
            f"D_{destinations_criteria[0]}_{destinations_criteria[1]}_"
            f"T_{time_string}"
        )
        self.logger.debug("Logging OD Cost Matrix results...")
        if USE_ARROW:
            output_od_lines = os.path.join(self.od_output_location, f"{out_filename}.at")
            self._export_to_arrow(output_od_lines)
        else:
            output_od_lines = os.path.join(self.od_output_location, f"{out_filename}.csv")
            self._export_to_csv(output_od_lines)

        self.logger.debug("Finished calculating OD cost matrix.")

    def _export_to_csv(self, out_csv_file):
        """Save the OD Lines result to a CSV file."""
        self.logger.debug(f"Saving OD cost matrix Lines output to CSV as {out_csv_file}.")

        # For services solve in pre-3.0 versions of ArcGIS Pro, export Origins and Destinations and properly populate
        # OriginOID and DestinationOID fields in the output Lines. Services do not preserve the original input OIDs,
        # instead resetting from 1, unlike solves using a local network dataset.  This issue was handled on the client
        # side in the ArcGIS Pro 3.1 release, but for older software, this extra post-processing step is necessary.
        if self.is_service and AnalysisHelpers.arcgis_version < "3.1":
            # Read the Lines output
            with self.solve_result.searchCursor(
                arcpy.nax.OriginDestinationCostMatrixOutputDataType.Lines, self.output_fields
            ) as cur:
                od_df = pd.DataFrame(cur, columns=self.output_fields)
            # Read the Origins output and transfer original OriginOID to Lines
            origins_columns = ["ObjectID", self.orig_origin_oid_field]
            with self.solve_result.searchCursor(
                arcpy.nax.OriginDestinationCostMatrixOutputDataType.Origins, origins_columns
            ) as cur:
                origins_df = pd.DataFrame(cur, columns=origins_columns)
            origins_df.set_index("ObjectID", inplace=True)
            od_df = od_df.join(origins_df, "OriginOID")
            del origins_df
            # Read the Destinations output and transfer original DestinationOID to Lines
            dest_columns = ["ObjectID", self.orig_dest_oid_field]
            with self.solve_result.searchCursor(
                arcpy.nax.OriginDestinationCostMatrixOutputDataType.Destinations, dest_columns
            ) as cur:
                dests_df = pd.DataFrame(cur, columns=dest_columns)
            dests_df.set_index("ObjectID", inplace=True)
            od_df = od_df.join(dests_df, "DestinationOID")
            del dests_df
            # Clean up and rename columns
            od_df.drop(["OriginOID", "DestinationOID"], axis="columns", inplace=True)
            od_df.rename(
                columns={self.orig_origin_oid_field: "OriginOID", self.orig_dest_oid_field: "DestinationOID"},
                inplace=True
            )
            # Write CSV file
            od_df.to_csv(out_csv_file, index=False)

        else:  # Local network dataset output
            with open(out_csv_file, "w", newline='') as f:
                writer = csv.writer(f)
                writer.writerow(self.output_fields)
                for row in self.solve_result.searchCursor(
                    arcpy.nax.OriginDestinationCostMatrixOutputDataType.Lines,
                    self.output_fields
                ):
                    writer.writerow(row)

        self.job_result["outputLines"] = out_csv_file

    def _export_to_arrow(self, out_arrow_file):
        """Save the OD Lines result to an Apache Arrow file."""
        self.logger.debug(f"Saving OD cost matrix Lines output to Apache Arrow as {out_arrow_file}.")
        self.solve_result.toArrowTable(
            arcpy.nax.OriginDestinationCostMatrixOutputDataType.Lines,
            self.output_fields,
            out_arrow_file
        )
        self.job_result["outputLines"] = out_arrow_file

    def _select_inputs(self, origins_criteria, destinations_criteria):
        """Create layers from the origins and destinations so the layers contain only the desired inputs for the chunk.

        Args:
            origins_criteria (list): Origin ObjectID range to select from the input dataset
            destinations_criteria ([type]): Destination ObjectID range to select from the input dataset
        """
        # Select the origins with ObjectIDs in this range
        self.logger.debug("Selecting origins for this chunk...")
        origins_where_clause = (
            f"{self.origins_oid_field_name} >= {origins_criteria[0]} "
            f"AND {self.origins_oid_field_name} <= {origins_criteria[1]}"
        )
        self.input_origins_layer_obj = run_gp_tool(
            arcpy.management.MakeFeatureLayer,
            [self.origins, self.input_origins_layer, origins_where_clause],
            log_to_use=self.logger
        ).getOutput(0)

        # Select the destinations with ObjectIDs in this range subject to the global destination where clause
        self.logger.debug("Selecting destinations for this chunk...")
        destinations_where_clause = (
            f"{self.destinations_oid_field_name} >= {destinations_criteria[0]} "
            f"AND {self.destinations_oid_field_name} <= {destinations_criteria[1]}"
        )
        if self.destination_where_clause:
            destinations_where_clause += f" AND {self.destination_where_clause}"
        self.input_destinations_layer_obj = run_gp_tool(
            arcpy.management.MakeFeatureLayer,
            [self.destinations, self.input_destinations_layer, destinations_where_clause],
            log_to_use=self.logger
        ).getOutput(0)

    def setup_logger(self, logger_obj):
        """Set up the logger used for logging messages for this process. Logs are written to a text file.

        Args:
            logger_obj: The logger instance.
        """
        logger_obj.setLevel(logging.DEBUG)
        if len(logger_obj.handlers) <= 1:
            file_handler = logging.FileHandler(self.log_file, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            logger_obj.addHandler(file_handler)
            formatter = logging.Formatter("%(process)d | %(message)s")
            file_handler.setFormatter(formatter)
            logger_obj.addHandler(file_handler)

    def teardown_logger(self):
        """Clean up and close the logger."""
        for handler in self.logger.handlers:
            handler.close()
            self.logger.removeHandler(handler)


def solve_od_cost_matrix(inputs, chunk):
    """Solve an OD Cost Matrix analysis for the given inputs for the given chunk of ObjectIDs.

    Args:
        inputs (dict): Dictionary of keyword inputs suitable for initializing the ODCostMatrix class
        chunk (list): Represents the ObjectID ranges to select from the origins and destinations when solving the OD
            Cost Matrix and the analysis start time of day. For example,
            [[1, 1000], [4001, 5000], datetime.datetime(2021, 6, 6, 8, 0, 0)] means use origin OIDs 1-1000 and
            destination OIDs 4001-5000 and a start time of 8:00 AM on June 6, 2021.

    Returns:
        dict: Dictionary of results from the ODCostMatrix class
    """
    odcm = ODCostMatrix(**inputs)
    odcm.logger.info((
        f"Processing origins OID {chunk[0][0]} to {chunk[0][1]} and destinations OID {chunk[1][0]} to {chunk[1][1]} "
        f"for start time {chunk[2]} as job id {odcm.job_id}"
    ))
    odcm.solve(chunk[0], chunk[1], chunk[2])
    odcm.teardown_logger()
    return odcm.job_result


class ParallelODCalculator():
    """Solves a large OD Cost Matrix by chunking the problem, solving in parallel, and combining results."""

    def __init__(  # pylint: disable=too-many-locals, too-many-arguments
        self, tool, origins, destinations, network_data_source, travel_mode, max_origins, max_destinations,
        time_window_start_day, time_window_start_time, time_window_end_day, time_window_end_time, time_increment,
        max_processes, time_units=None, cutoff=None, weight_field=None, barriers=None,
        out_csv_file=None, out_na_folder=None
    ):
        """Compute OD Cost Matrices between Origins and Destinations in parallel for all increments in the time window.

        Post-processes output according to which tool is being run.  For Calculate Accessibility Matrix, calculate
        accessibility statistics and write the output fields to the output Origins feature class.

        This class assumes that the inputs have already been pre-processed and validated.

        Args:
            tool (str): Enum string name corresponding to a value in AnalysisHelpers.ODTool
            origins (str): Catalog path to origins
            destinations (str): Catalog path to destinations
            network_data_source (str): Network data source catalog path or URL
            travel_mode (str): String-based representation of a travel mode (name or JSON)
            max_origins (int): Maximum origins allowed in a chunk
            max_destinations (int): Maximum destinations allowed in a chunk
            time_window_start_day (str): English weekday name or YYYYMMDD date representing the weekday or start date of
                the time window
            time_window_start_time (str): HHMM time of day for the start of the time window
            time_window_end_day (str): English weekday name or YYYYMMDD date representing the weekday or end date of
                the time window
            time_window_end_time (str): HHMM time of day for the end of the time window
            time_increment (int): Number of minutes between each run of the OD Cost Matrix in the time window
            max_processes (int): Maximum number of parallel processes allowed
            time_units (str, optional): String representation of time units
            cutoff (float, optional): Time cutoff to limit the OD Cost Matrix solve. Interpreted in the time_units.
            weight_field (str, optional): Field in the destinations to use as a weight for the number of destinations
                at each location. For example, the number of jobs at that location. When not provided, all destinations
                count as 1.
            barriers (list(str), optional): List of catalog paths to point, line, and polygon barriers to use.
                Defaults to None.
            out_csv_file (str, optional): Catalog path to the output CSV file for calculated statistics
        """
        self.tool = AnalysisHelpers.ODTool[tool]
        self.origins = origins
        self.destinations = destinations
        if time_units:
            time_units = AnalysisHelpers.convert_time_units_str_to_enum(time_units)
        if cutoff == "":
            cutoff = None
        if not barriers:
            barriers = []
        self.max_processes = max_processes
        self.weight_field = weight_field
        self.out_csv_file = out_csv_file

        # Validate time window inputs and convert them into a list of times of day to run the analysis
        try:
            self.start_times = AnalysisHelpers.make_analysis_time_of_day_list(
                time_window_start_day, time_window_end_day, time_window_start_time, time_window_end_time,
                time_increment
            )
        except Exception as ex:
            err = "Error parsing input time window."
            LOGGER.error(err)
            LOGGER.error(str(ex))
            raise ValueError from ex

        # Scratch folder to store intermediate outputs from the OD Cost Matrix processes
        unique_id = uuid.uuid4().hex
        self.scratch_folder = os.path.join(
            arcpy.env.scratchFolder, "CalcAccMtx_" + unique_id)  # pylint: disable=no-member
        LOGGER.info(f"Intermediate outputs will be written to {self.scratch_folder}.")
        os.mkdir(self.scratch_folder)
        if out_na_folder:
            os.mkdir(out_na_folder)
        self.od_output_location = out_na_folder if out_na_folder else self.scratch_folder

        # Set up a where clause to eliminate destinations that will never contribute any values to the final solution.
        # Only applies if we're using a weight field.
        if self.weight_field:
            self.dest_where = f"{self.weight_field} IS NOT NULL and {self.weight_field} <> 0"
        else:
            self.dest_where = ""

        # Initialize the dictionary of inputs to send to each OD solve
        self.od_inputs = {
            "tool": self.tool,
            "origins": self.origins,
            "destinations": self.destinations,
            "destination_where_clause": self.dest_where,
            "network_data_source": network_data_source,
            "travel_mode": travel_mode,
            "scratch_folder": self.scratch_folder,
            "od_output_location": self.od_output_location,
            "time_units": time_units,
            "cutoff": cutoff,
            "barriers": barriers
        }

        # Construct OID ranges for chunks of origins and destinations
        self.origin_ranges = self._get_oid_ranges_for_input(self.origins, max_origins)
        destination_ranges = self._get_oid_ranges_for_input(self.destinations, max_destinations, self.dest_where)

        # Construct chunks consisting of (range of origin oids, range of destination oids, start time)
        self.chunks = itertools.product(self.origin_ranges, destination_ranges, self.start_times)
        # Calculate the total number of jobs to use in logging
        self.total_jobs = len(self.origin_ranges) * len(destination_ranges) * len(self.start_times)

        self.od_line_files = []

    def _validate_od_settings(self):
        """Validate OD cost matrix settings before spinning up a bunch of parallel processes doomed to failure."""
        # Create a dummy ODCostMatrix object, initialize an OD solver object, and set properties. This allows us to
        # detect any errors prior to spinning up a bunch of parallel processes and having them all fail.
        LOGGER.debug("Validating OD Cost Matrix settings...")
        odcm = None
        try:
            odcm = ODCostMatrix(**self.od_inputs)
            odcm.initialize_od_solver()
            LOGGER.debug("OD Cost Matrix settings successfully validated.")
        except Exception:
            LOGGER.error("Error initializing OD Cost Matrix analysis.")
            errs = traceback.format_exc().splitlines()
            for err in errs:
                LOGGER.error(err)
            raise
        finally:
            if odcm:
                # Close logging and delete the temporary log file
                odcm.teardown_logger()
                os.remove(odcm.log_file)

    @staticmethod
    def _get_oid_ranges_for_input(input_fc, max_chunk_size, where=""):
        """Construct ranges of ObjectIDs for use in where clauses to split large data into chunks.

        Args:
            input_fc (str, layer): Data that needs to be split into chunks
            max_chunk_size (int): Maximum number of rows that can be in a chunk
            where (str, optional): Where clause to use to filter data before chunking. Defaults to "".

        Returns:
            list: list of ObjectID ranges for the current dataset representing each chunk. For example,
                [[1, 1000], [1001, 2000], [2001, 2478]] represents three chunks of no more than 1000 rows.
        """
        ranges = []
        num_in_range = 0
        current_range = [0, 0]
        # Loop through all OIDs of the input and construct tuples of min and max OID for each chunk
        # We do it this way and not by straight-up looking at the numerical values of OIDs to account
        # for definition queries, selection sets, or feature layers with gaps in OIDs
        for row in arcpy.da.SearchCursor(input_fc, "OID@", where):  # pylint: disable=no-member
            oid = row[0]
            if num_in_range == 0:
                # Starting new range
                current_range[0] = oid
            # Increase the count of items in this range and set the top end of the range to the current oid
            num_in_range += 1
            current_range[1] = oid
            if num_in_range == max_chunk_size:
                # Finishing up a chunk
                ranges.append(current_range)
                # Reset range trackers
                num_in_range = 0
                current_range = [0, 0]
        # After looping, close out the last range if we still have one open
        if current_range != [0, 0]:
            ranges.append(current_range)

        return ranges

    def solve_od_in_parallel(self):
        """Solve the OD Cost Matrix in chunks and post-process the results."""
        # Validate OD Cost Matrix settings. Essentially, create a dummy ODCostMatrix class instance and set up the
        # solver object to ensure this at least works. Do this up front before spinning up a bunch of parallel
        # processes that are guaranteed to all fail.
        self._validate_od_settings()

        # Compute OD cost matrix in parallel
        LOGGER.info(f"Solving OD Cost Matrix in parallel ({self.total_jobs} chunks)...")
        completed_jobs = 0  # Track the number of jobs completed so far to use in logging
        # Use the concurrent.futures ProcessPoolExecutor to spin up parallel processes that solve the OD cost
        # matrices
        with futures.ProcessPoolExecutor(max_workers=self.max_processes) as executor:
            # Each parallel process calls the solve_od_cost_matrix() function with the od_inputs dictionary for the
            # given origin and destination OID ranges and time of day.
            jobs = {executor.submit(
                solve_od_cost_matrix, self.od_inputs, chunks): chunks for chunks in self.chunks}
            # As each job is completed, add some logging information and store the results to post-process later
            for future in futures.as_completed(jobs):
                completed_jobs += 1
                LOGGER.info(
                    f"Finished OD Cost Matrix calculation {completed_jobs} of {self.total_jobs}.")
                try:
                    # The OD cost matrix job returns a results dictionary. Retrieve it.
                    result = future.result()
                except Exception:
                    # If we couldn't retrieve the result, some terrible error happened. Log it.
                    LOGGER.error("Failed to get OD Cost Matrix result from parallel processing.")
                    errs = traceback.format_exc().splitlines()
                    for err in errs:
                        LOGGER.error(err)
                    raise

                # Parse the results dictionary and store components for post-processing.
                if result["solveSucceeded"]:
                    self.od_line_files.append(result["outputLines"])
                else:
                    # Typically, a solve fails because no destinations were found for any of the origins in the chunk,
                    # and this is a perfectly legitimate failure. It is not an error. However, they may be other, less
                    # likely, reasons for solve failure. Write solve messages to the main GP message thread in debug
                    # mode only in case the user is having problems. The user can also check the individual OD log
                    # files.
                    LOGGER.debug(f"Solve failed for job id {result['jobId']}.")
                    LOGGER.debug(result["solveMessages"])

        # Post-process and write out results according to which tool is being run
        if self.od_line_files:
            self.od_line_files = sorted(self.od_line_files)
            if self.tool is AnalysisHelpers.ODTool.CalculateAccessibilityMatrix:
                # Calculate statistics from the results of the OD Cost Matrix calculations
                # and write them to the output fields in the Origins table.
                self._calculate_accessibility_matrix_outputs()
            elif self.tool is AnalysisHelpers.ODTool.CalculateTravelTimeStatistics:
                self._calculate_travel_time_statistics_outputs()
            if self.od_output_location != self.scratch_folder:
                LOGGER.info(f"Individual network analysis results written to {self.od_output_location}.")
        else:
            LOGGER.warning("All OD Cost Matrix solves failed, so no output was produced.")

        # Cleanup
        # Delete the job folders if the job succeeded
        if DELETE_INTERMEDIATE_OD_OUTPUTS:
            LOGGER.info("Deleting intermediate outputs...")
            try:
                shutil.rmtree(self.scratch_folder, ignore_errors=True)
            except Exception:  # pylint: disable=broad-except
                # If deletion doesn't work, just throw a warning and move on. This does not need to kill the tool.
                LOGGER.warning(f"Unable to delete intermediate OD Cost Matrix output folder {self.scratch_folder}.")

        LOGGER.info("Finished calculating OD Cost Matrices.")

    def _calculate_accessibility_matrix_outputs(self):
        """Calculate accessibility statistics and write them to the Origins table."""
        LOGGER.info("Calculating statistics for final output...")

        # Read the result files from each individual OD and combine them together into a
        # dataframe with the number of time each origin reached each destination
        if USE_ARROW:
            LOGGER.debug("Reading results into dataframe from Arrow tables...")
        else:
            LOGGER.debug("Reading results into dataframe from CSV files...")
        t0 = time.time()
        result_df = None
        for od_file in self.od_line_files:
            if USE_ARROW:
                with pa.memory_map(od_file, 'r') as source:
                    batch_reader = pa.ipc.RecordBatchFileReader(source)
                    chunk_table = batch_reader.read_all()
                df = chunk_table.to_pandas(split_blocks=True, zero_copy_only=True)
            else:
                df = pd.read_csv(od_file, dtype={"OriginOID": int, "DestinationOID": int})

            df["TimesReached"] = 1
            df.set_index(["OriginOID", "DestinationOID"], inplace=True)

            if result_df is None:
                # Initialize the big combined dataframe if this is the first one
                result_df = df
                continue

            # Add the current results dataframe to the big combined one and sum the number of times reached so
            # far for each OD pair
            result_df = pd.concat([result_df, df]).groupby(["OriginOID", "DestinationOID"]).sum()
            del df

        result_df.reset_index(inplace=True)

        LOGGER.debug(f"Time to read all OD result files: {time.time() - t0}")

        # Handle accounting for the actual number of destinations
        if self.weight_field:
            # Read in the weight field values and join them into the result table
            LOGGER.debug("Joining weight field from destinations to results dataframe...")
            with arcpy.da.SearchCursor(  # pylint: disable=no-member
                    self.destinations, ["OID@", self.weight_field]) as cur:
                w_df = pd.DataFrame(cur, columns=["DestinationOID", "Weight"])

            # Calculate the total number of destinations based on weight and store this for later use
            total_dests = w_df["Weight"].sum()

            # Join the Weight field into the results dataframe
            w_df.set_index("DestinationOID", inplace=True)
            result_df = result_df.join(w_df, "DestinationOID")
            del w_df

            # We don't need this field anymore
            result_df.drop(["DestinationOID"], axis="columns", inplace=True)

        else:
            # Count every row as 1 since we're not using a weight field
            result_df["Weight"] = 1

            # Set the total number of destinations to the number of rows in the destinations table.
            total_dests = int(arcpy.management.GetCount(self.destinations).getOutput(0))

        # Create the output dataframe indexed by the OriginOID
        LOGGER.debug("Creating output dataframe indexed by OriginOID...")
        unique = result_df["OriginOID"].unique()
        output_df = pd.DataFrame(unique, columns=["OriginOID"])
        del unique
        output_df.set_index("OriginOID", inplace=True)

        # Calculate the total destinations found for each origin using the weight field
        LOGGER.debug("Calculating TotalDests and PercDests...")
        output_df["TotalDests"] = result_df[result_df["TimesReached"] > 0].groupby("OriginOID")["Weight"].sum()
        # Calculate the percentage of destinations reached
        output_df["PercDests"] = 100.0 * output_df["TotalDests"] / total_dests

        # Determine the TotalDests field type because this affects the output field type to use
        if pd.api.types.is_integer_dtype(output_df["TotalDests"]):
            num_dest_field_type = "LONG"
        else:
            num_dest_field_type = "DOUBLE"

        # Calculate the number of destinations accessible at different thresholds
        LOGGER.debug("Calculating the number of destinations accessible at different thresholds...")
        field_defs = [["TotalDests", num_dest_field_type], ["PercDests", "DOUBLE"]]
        for perc in range(10, 100, 10):
            total_field = f"DsAL{perc}Perc"
            perc_field = f"PsAL{perc}Perc"
            field_defs += [[total_field, num_dest_field_type], [perc_field, "DOUBLE"]]
            threshold = len(self.start_times) * perc / 100
            output_df[total_field] = result_df[result_df["TimesReached"] >= threshold].groupby(
                "OriginOID")["Weight"].sum()
            output_df[perc_field] = 100.0 * output_df[total_field] / total_dests
        # Fill empty cells with 0
        output_df.fillna(0, inplace=True)
        # Clean up
        del result_df

        # Append the calculated transit frequency statistics to the output feature class
        LOGGER.debug("Writing data to output Origins...")
        arcpy.management.AddFields(self.origins, field_defs)
        oid_field = arcpy.Describe(self.origins).oidFieldName
        fields = [oid_field] + [f[0] for f in field_defs]
        with arcpy.da.UpdateCursor(self.origins, fields) as cur:  # pylint: disable=no-member
            for row in cur:
                oid = row[0]
                try:
                    new_row = [oid] + output_df.loc[oid].to_list()
                except KeyError:
                    # Fill null values with 0 where appropriate if the feature wasn't even in the dataframe.
                    new_row = [oid] + [0] * len(field_defs)
                cur.updateRow(new_row)

        LOGGER.info(f"Accessibility statistics fields were added to Origins table {self.origins}.")

    def _calculate_travel_time_statistics_outputs(self):
        """Calculate travel time statistics and write them to the output file."""
        # NOTE: This method does not support Arrow outputs at this time.  If reimplementing the USE_ARROW option,
        # this method needs to be updated.
        LOGGER.info("Calculating travel time statistics...")
        t0 = time.time()

        # Clean up any existing output if necessary
        if os.path.exists(self.out_csv_file):
            os.remove(self.out_csv_file)

        # Read the output CSV files into a pandas dataframe to calculate statistics
        # Handle each origin range separately to avoid pulling all results into memory at once
        first = True
        for origin_range in self.origin_ranges:
            files_for_origin_range = [
                f for f in self.od_line_files if os.path.basename(f).startswith(
                    f"ODLines_O_{origin_range[0]}_{origin_range[1]}_"
                )
            ]
            if not files_for_origin_range:
                # No results for this chunk
                continue

            mapfunc = partial(
                pd.read_csv,
                dtype={"OriginOID": int, "DestinationOID": int, "Total_Time": float}
            )
            df = pd.concat(map(mapfunc, files_for_origin_range), ignore_index=True)

            # Calculate simple stats
            stats = df.groupby(["OriginOID", "DestinationOID"]).agg(
                {"Total_Time": ["count", "min", "max", "mean"]}
            )
            stats.columns = stats.columns.droplevel(0)
            stats.reset_index(inplace=True)

            if first:
                # Create the output CSV file
                stats.to_csv(self.out_csv_file, index=False)
                first = False
            else:
                # Append results to the existing output CSV file
                stats.to_csv(self.out_csv_file, mode='a', index=False, header=False)

        LOGGER.debug(f"Time to read all OD result files and calculate statistics: {time.time() - t0}")
        LOGGER.info(f"Travel time statistics written to {self.out_csv_file}.")


def launch_parallel_od():
    """Read arguments passed in via subprocess and run the parallel OD Cost Matrix.

    This script is intended to be called via subprocess via the CalculateAccessibilityMatrixInParallel.py module, which
    does essential preprocessing and validation. Users should not call this script directly from the command line.

    We must launch this script via subprocess in order to support parallel processing from an ArcGIS Pro script tool,
    which cannot do parallel processing directly.
    """
    # Create the parser
    parser = argparse.ArgumentParser(description=globals().get("__doc__", ""), fromfile_prefix_chars='@')

    # Define Arguments supported by the command line utility

    # --tool parameter
    help_string = "Enum string name corresponding to a value in AnalysisHelpers.ODTool."
    parser.add_argument("-t", "--tool", action="store", dest="tool", help=help_string, required=True)

    # --origins parameter
    help_string = "The full catalog path to the feature class containing the origins. Output fields will be added."
    parser.add_argument("-o", "--origins", action="store", dest="origins", help=help_string, required=True)

    # --destinations parameter
    help_string = "The full catalog path to the feature class containing the destinations."
    parser.add_argument("-d", "--destinations", action="store", dest="destinations", help=help_string, required=True)

    # --network-data-source parameter
    help_string = "The full catalog path to the network dataset or a portal url that will be used for the analysis."
    parser.add_argument(
        "-n", "--network-data-source", action="store", dest="network_data_source", help=help_string, required=True)

    # --travel-mode parameter
    help_string = (
        "The name or JSON string representation of the travel mode from the network data source that will be used for "
        "the analysis."
    )
    parser.add_argument("-tm", "--travel-mode", action="store", dest="travel_mode", help=help_string, required=True)

    # --time-units parameter
    help_string = "String name of the time units for the analysis. These units will be used in the output."
    parser.add_argument("-tu", "--time-units", action="store", dest="time_units", help=help_string, required=False)

    # --max-origins parameter
    help_string = (
        "Maximum number of origins that can be in one chunk for parallel processing of OD Cost Matrix solves. "
        "For example, 1000 means that a chunk consists of no more than 1000 origins and max-destination destinations."
    )
    parser.add_argument(
        "-mo", "--max-origins", action="store", dest="max_origins", type=int, help=help_string, required=True)

    # --max-destinations parameter
    help_string = (
        "Maximum number of destinations that can be in one chunk for parallel processing of OD Cost Matrix solves. "
        "For example, 1000 means that a chunk consists of no more than max-origin origins and 1000 destinations."
    )
    parser.add_argument(
        "-md", "--max-destinations", action="store", dest="max_destinations", type=int, help=help_string, required=True)

    # --max-processes parameter
    help_string = "Maximum number parallel processes to use for the OD Cost Matrix solves."
    parser.add_argument(
        "-mp", "--max-processes", action="store", dest="max_processes", type=int, help=help_string, required=True)

    # --time-window-start-day parameter
    help_string = "Time window start day of week or YYYYMMDD date."
    parser.add_argument("-twsd", "--time-window-start-day", action="store", dest="time_window_start_day",
                        help=help_string, required=True)

    # --time-window-start-time parameter
    help_string = "Time window start time as hh:mm."
    parser.add_argument("-twst", "--time-window-start-time", action="store", dest="time_window_start_time",
                        help=help_string, required=True)

    # --time-window-end-day parameter
    help_string = "Time window end day of week or YYYYMMDD date."
    parser.add_argument("-twed", "--time-window-end-day", action="store", dest="time_window_end_day",
                        help=help_string, required=True)

    # --time-window-end-time parameter
    help_string = "Time window end time as hh:mm."
    parser.add_argument("-twet", "--time-window-end-time", action="store", dest="time_window_end_time",
                        help=help_string, required=True)

    # --time-increment
    help_string = "Time increment in minutes"
    parser.add_argument("-ti", "--time-increment", action="store", dest="time_increment", type=int,
                        help=help_string, required=True)

    # --cutoff parameter
    help_string = (
        "Impedance cutoff to limit the OD cost matrix search distance. Should be specified in the same units as the "
        "time-units parameter"
    )
    parser.add_argument(
        "-co", "--cutoff", action="store", dest="cutoff", type=float, help=help_string, required=False)

    # --weight-field parameter
    help_string = "The name of the field in the input destinations that indicates the destination's weight."
    parser.add_argument(
        "-wf", "--weight-field", action="store", dest="weight_field", help=help_string, required=False)

    # --barriers parameter
    help_string = "A list of catalog paths to the feature classes containing barriers to use in the OD Cost Matrix."
    parser.add_argument(
        "-b", "--barriers", action="store", dest="barriers", help=help_string, nargs='*', required=False)

    # --out-csv-file parameter
    help_string = "Catalog path for the output CSV file."
    parser.add_argument(
        "-oc", "--out-csv-file", action="store", dest="out_csv_file", help=help_string, required=False)

    # --out-na-folder parameter
    help_string = "Catalog path for a folder to store the outputs of the network analysis."
    parser.add_argument(
        "-of", "--out-na-folder", action="store", dest="out_na_folder", help=help_string, required=False)

    try:
        # Get arguments as dictionary.
        args = vars(parser.parse_args())

        # Initialize a parallel OD Cost Matrix calculator class
        od_calculator = ParallelODCalculator(**args)
        # Solve the OD Cost Matrix in parallel chunks
        start_time = time.time()
        od_calculator.solve_od_in_parallel()
        LOGGER.info(
            f"Parallel OD Cost Matrix calculation completed in {round((time.time() - start_time) / 60, 2)} minutes")
    except Exception:  # pylint: disable=broad-except
        errs = traceback.format_exc().splitlines()
        for err in errs:
            LOGGER.error(err)
        raise


if __name__ == "__main__":
    # This script should always be launched via subprocess as if it were being called from the command line.
    launch_parallel_od()
