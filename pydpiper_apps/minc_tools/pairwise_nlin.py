#!/usr/bin/env python

from pydpiper.application import AbstractApplication
import pydpiper.file_handling as fh
import pydpiper_apps.minc_tools.registration_functions as rf
import pydpiper_apps.minc_tools.registration_file_handling as rfh
import pydpiper_apps.minc_tools.minc_modules as mm
import pydpiper_apps.minc_tools.minc_atoms as ma
import pydpiper_apps.minc_tools.stats_tools as st
import pydpiper_apps.minc_tools.option_groups as og
import pydpiper_apps.minc_tools.old_MBM_interface_functions as ombm
import Pyro
from optparse import OptionGroup
from datetime import date
from os.path import abspath, isdir
import logging
import sys

logger = logging.getLogger(__name__)

Pyro.config.PYRO_MOBILE_CODE=1 

class PairwiseNonlinear(AbstractApplication):
    def setup_options(self):
        group = OptionGroup(self.parser, "Pairwise non-linear options", 
                        "Options for pairwise non-linear registration of lsq6 or lsq12 aligned brains.")
        group.add_option("--input-space", dest="input_space",
                      type="string", default="lsq6", 
                      help="Option to specify space of input-files. Can be lsq6 (default), lsq12 or native.")
        self.parser.add_option_group(group)
        """Add option groups from specific modules"""
        rf.addGenRegOptionGroup(self.parser)
        og.tmpLongitudinalOptionGroup(self.parser)
        st.addStatsOptions(self.parser)
        
        self.parser.set_usage("%prog [options] input files") 

    def setup_appName(self):
        appName = "Pairwise-nonlinear"
        return appName

    def run(self):
        
        """Directory handling etc as in MBM"""
        if not self.options.pipeline_name:
            pipeName = str(date.today()) + "_pipeline"
        else:
            pipeName = self.options.pipeline_name
        
        processedDirectory = fh.createSubDir(self.outputDir, pipeName + "_processed")
        
        """Check that correct registration method was specified"""
        if self.options.reg_method != "minctracc" and self.options.reg_method != "mincANTS":
            logger.error("Incorrect registration method specified: " + self.options.reg_method)
            sys.exit()
        
        """Create file handling classes for each image"""
        inputs = rf.initializeInputFiles(self.args, processedDirectory, self.options.mask_dir)
        
        """Put blurs into array"""
        blurs = []
        for i in self.options.stats_kernels.split(","):
            blurs.append(float(i))
        
        """Create file handler for nlin average from MBM"""
        if self.options.nlin_avg:
            nlinFH = rfh.RegistrationFHBase(abspath(self.options.nlin_avg), processedDirectory)
        else:
            nlinFH = None
        if self.options.mbm_dir and not isdir(abspath(self.options.mbm_dir)):
            logger.error("The --mbm-directory specified does not exist: " + abspath(self.options.mbm_dir))
            sys.exit()
        
        """Get transforms from inputs to final nlin average and vice versa as well as lsq6 files"""
        if self.options.nlin_avg and self.options.mbm_dir:
            xfmsPipe = ombm.getXfms(nlinFH, inputs, self.options.input_space, abspath(self.options.mbm_dir))
            if len(xfmsPipe.stages) > 0:
                self.pipeline.addPipeline(xfmsPipe)
        else:
            logger.info("MBM directory and nlin_average not specified.")
            logger.info("Calculating pairwise nlin only without resampling to common space.")
        
        """Create a dictionary of statistics. Each subject gets an array of statistics
           indexed by inputFile."""
        subjectStats = {}
        
        """Register each image with every other image."""
        for inputFH in inputs:
            subjectStats[inputFH] = {}
            for targetFH in inputs:
                if inputFH != targetFH:
                # MF TODO: Make generalization of registration parameters easier. 
                    if self.options.reg_method == "mincANTS":
                        """First, run a standard lsq12 registration on the brains if input is lsq6."""
                        if self.options.input_space=="lsq6":
                            lsq12 = mm.LSQ12(inputFH, targetFH)
                            self.pipeline.addPipeline(lsq12.p)
                        """Then run a single generation mincANTS call"""
                        b = 0.056  
                        self.pipeline.addStage(ma.blur(inputFH, b, gradient=True))
                        self.pipeline.addStage(ma.blur(targetFH, b, gradient=True))              
                        self.pipeline.addStage(ma.mincANTS(inputFH, 
                                                           targetFH,
                                                           blur=[-1,b]))
                    elif self.options.reg_method == "minctracc":
                        hm = mm.HierarchicalMinctracc(inputFH, targetFH)
                        self.pipeline.addPipeline(hm.p)
                    if nlinFH:
                        resample = ma.mincresample(inputFH, targetFH, likeFile=nlinFH)
                    else:
                        resample = ma.mincresample(inputFH, targetFH, likeFile=inputFH)
                    self.pipeline.addStage(resample)
                    inputFH.setLastBasevol(resample.outputFiles[0])
                    """Calculate statistics"""
                    stats = st.CalcChainStats(inputFH, targetFH, blurs)
                    stats.calcFullDisplacement()
                    stats.calcDetAndLogDet(useFullDisp=True)
                    self.pipeline.addPipeline(stats.p)
                    subjectStats[inputFH][targetFH] = stats.statsGroup
                    """Resample to nlin space from previous build model run, if specified"""
                    if self.options.nlin_avg and self.options.mbm_dir:
                        xfmToNlin = inputFH.getLastXfm(nlinFH, groupIndex=0)
                        for b in blurs:
                            res = ombm.resampleToCommon(xfmToNlin, inputFH, subjectStats[inputFH][targetFH], b, nlinFH)
                            self.pipeline.addPipeline(res)
                    """Reset last base volume to original input before continuing to next pair in loop."""
                    inputFH.setLastBasevol()

if __name__ == "__main__":
    
    application = PairwiseNonlinear()
    application.start()
