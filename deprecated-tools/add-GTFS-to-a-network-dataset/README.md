# add-GTFS-to-a-network-dataset

*Add GTFS to a Network Dataset* allows you to put GTFS public transit data into an ArcGIS network dataset so you can run schedule-aware analyses using the Network Analyst tools, like Service Area, OD Cost Matrix, and Location-Allocation.

**This tool is deprecated.** The tool author will no longer be making further enhancements or fixing major bugs. Instead, you can create and use transit-enabled network datasets in ArcGIS Pro 2.4 or higher without the need to download any additional tools. [Learn more about network analysis with public transit in ArcGIS Pro.](https://pro.arcgis.com/en/pro-app/help/analysis/networks/network-analysis-with-public-transit-data.htm)

The supplemental "Transit Analysis Tools.tbx" toolbox was formerly part of this toolset, but it is now [separate](https://github.com/Esri/public-transit-tools/blob/master/deprecated-tools/transit-network-analysis-tools-arcmap).

## Features
* Use schedule-based transit data with the ArcGIS Network Analyst tools.
* Create transit service areas (transitsheds).
* Study accessibility of destinations by transit.
* Make location decisions based on access by transit.
* ArcGIS toolbox - No coding is required to use this tool.  Just add the toolbox to ArcMap and use the tools like any other geoprocessing tools.

## Instructions

If you just want to run this tool in a ready-to-use format, don't use this GitHub version.  Instead, [download it from ArcGIS Online](http://arcg.is/10jXez) and follow the instructions in the [User's Guide](https://github.com/Esri/public-transit-tools/blob/master/deprecated-tools/add-GTFS-to-a-network-dataset/UsersGuide.md).

This GitHub repo is meant primarily for the author's own development and for those rare brave souls who actually want to look at the transit evaluator's ArcObjects code.

If you want to play with the code, fork it and have fun.  In addition to grabbing everything from GitHub, you also need to download and set up the correct version of [System.Data.SQLite](https://system.data.sqlite.org/index.html/doc/trunk/www/downloads.wiki).
  - The one you want is sqlite-netFx20-binary-Win32-2005-1.0.98.0.zip.  Download this.
  - Unzip it to a folder called SQLite in the same directory as the TransitEvaluator and GetEIDs folders.
  - In the Debug folder where TransitEvaluator.sln is going to build, create a folder called x86.
  - Copy SQLite.Interop.dll from your SQLite folder into the x86 folder.
  - If you want the Add GTFS to a Network Dataset tool to work in ArcGIS Server, you will also need to download the 64-bit version of the above and place the 64-bit version of SQLite.Interop.dll in a folder called x64.

To build the GetEIDs tool, you will need to do the same thing with the System.Data.SQLite files and the GetEIDs Debug folder.

## Requirements

* ArcMap 10.1 or higher with a Desktop Standard (ArcEditor) license. (You can still use it if you have a Desktop Basic license, but you will have to find an alternate method for one of the pre-processing tools.) ArcMap 10.6 or higher is recommended because you will be able to construct your network dataset much more easily using a template rather than having to do it manually step by step.
  * This tool does not work in ArcGIS Pro. You can create and use transit-enabled network datasets in ArcGIS Pro 2.4 or higher without the need to download any additional tools. [Learn more about network analysis with public transit in ArcGIS Pro.](https://pro.arcgis.com/en/pro-app/help/analysis/networks/network-analysis-with-public-transit-data.htm)
* Network Analyst extension.
* Street data for the area covered by your transit system, preferably data including pedestrian attributes.
* A valid GTFS dataset. If your GTFS dataset has blank values for arrival_time and departure_time in stop_times.txt, you will not be able to run this tool.
* The necessary privileges to install something on your computer.

## Resources

* [User's Guide](https://github.com/Esri/public-transit-tools/blob/master/deprecated-tools/add-GTFS-to-a-network-dataset/UsersGuide.md)
* [Troubleshooting Guide](https://github.com/Esri/public-transit-tools/blob/master/deprecated-tools/add-GTFS-to-a-network-dataset/TroubleshootingGuide.md)
* [GTFS specification](https://github.com/google/transit/blob/master/gtfs/spec/en/reference.md)

## Issues

Find a bug or want to request a new feature?  Please let us know by submitting an issue, or post a question in the [Esri Community forums](https://community.esri.com/t5/public-transit-questions/bd-p/public-transit-questions).

## Contributing

Esri welcomes contributions from anyone and everyone. Please see our [guidelines for contributing](https://github.com/esri/contributing).

## Licensing
Copyright 2019 Esri

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

A copy of the license is available in the repository's [license.txt](../License.txt?raw=true) file.