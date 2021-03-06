from pydpiper.pipeline import Pipeline, CmdStage, InputFile, OutputFile, LogFile
from atoms_and_modules.registration_functions import isFileHandler
from atoms_and_modules.minc_atoms import mincAverageDisp, xfmConcat, xfmInvert
import pydpiper.file_handling as fh
from optparse import OptionGroup
from os.path import abspath
import sys
import Pyro

Pyro.config.PYRO_MOBILE_CODE=1

def addStatsOptions(parser):
    group = OptionGroup(parser, "Statistics options", 
                        "Options for calculating statistics.")
    group.add_option("--stats-kernels", dest="stats_kernels",
                      type="string", default="1.0,0.5,0.2,0.1", 
                      help="comma separated list of blurring kernels for analysis. Default is: 1.0,0.5,0.2,0.1")
    parser.add_option_group(group)
    
def createInvXfmName(iFH, xfm):
    invXfmBase = fh.removeBaseAndExtension(xfm).split(".xfm")[0]
    invXfm = fh.createBaseName(iFH.transformsDir, invXfmBase + "_inverted.xfm")
    return invXfm

def createPureNlinXfmName(iFH, xfm):
    nlinBase = fh.removeBaseAndExtension(xfm) + "_pure_nlin.xfm"
    nlinXfm = fh.createBaseName(iFH.tmpDir, nlinBase)
    return nlinXfm

def setDispName(iFH, xfm, defaultDir):
        outDir = iFH.setOutputDirectory(defaultDir)
        outBase = fh.removeBaseAndExtension(xfm) + "_displacement.mnc"
        outputFile = fh.createBaseName(outDir, outBase)
        return outputFile  

class StatsGroup(object):
    """This group saves the key output from each instance for CalcStats, 
       so it can easily be retrieved later."""
    def __init__(self):
        self.jacobians = {}
        self.scaledJacobians = {}        

class CalcStats(object):
    """Statistics calculation between an input and target. 
       This class calculates multiple displacement fields, jacobians and scaled jacobians.
       It should be called once for each inputFH in your pipeline.  
       General functionality as follows:
       1. Class instantiated with input, target and blurs. May optionally specify
          array of input file handlers so that re-centering can be appropriately
          calculated. A scalingFactor may also be specified, for calculating the
          scaled jacobian determinants, as is described in the __init__
          function and elsewhere in the code.  
       2. If needed, invert transform between input and target in setupXfms()
       3. Call fullStatsCalc in calling class, which calculates linear and 
          pure nonlinear displacement, as well as re-centering average, before
          calculating determinants and log determinants. 
       4. Alternate usage is to call calcFullDisplacement followed by calcDetAndLogDet, 
          which will use full displacement (rather than just non-linear component)
          for calculating determinants.   
    """
    def __init__(self, inputFH, targetFH, blurs, inputArray=None, scalingFactor=None):
        self.p = Pipeline()
        self.inputFH = inputFH
        self.targetFH = targetFH
        self.blurs = blurs
        self.statsGroup = StatsGroup()
        self.setupXfms()
        """Optional inputArray used to calculate an average displacement and use for recentering."""
        if inputArray:
            self.dispToAvg = []
            self.setupDispArray(inputArray)
        else:
            self.dispToAvg = None
        """   
            Specify an optional xfm to be used when calculating the 
            scaled jacobians. This jacobian will then be concatenated with the
            self.linearXfm, the linear portion of the final non-linear transform from input to target.
            
            A  
            
        """
        self.scalingFactor = scalingFactor
        
    def setupXfms(self):
        self.xfm = self.inputFH.getLastXfm(self.targetFH)
        if not self.xfm:
            print "Cannot calculate statistics. No transform between input and target specified."
            sys.exit()
        """Check for existence of inverse transform. If it doesn't exist, create it. """
        self.invXfm = self.targetFH.getLastXfm(self.inputFH)
        if not self.invXfm:
            xi = xfmInvert(self.xfm, FH=self.inputFH)
            self.p.addStage(xi)
            self.invXfm = xi.outputFiles[0]
    
    def setupDispArray(self, inputArray):
        """NOTE: inputArray must be an array of file handlers. """
        for iFH in inputArray:
            """Check to see if invXfm exists. If not, create name (but we don't actually need
               to construct the command here, as this will happen in its own CalcStats class)"""
            xfm = iFH.getLastXfm(self.targetFH)
            invXfm = self.targetFH.getLastXfm(iFH)
            if not invXfm:
                invXfm = createInvXfmName(iFH, xfm)
            nlinXfm = createPureNlinXfmName(iFH, invXfm)
            """Here we are assuming that the pure nlin displacement goes in the tmp directory.
               If we change this when the actual calculation is done, we do it here too. 
            """
            nlinDisp = setDispName(iFH, nlinXfm, "tmp")
            self.dispToAvg.append(nlinDisp)
    
    def fullStatsCalc(self):
        self.linAndNlinDisplacement()
        self.calcDetAndLogDet()  
    
    def calcFullDisplacement(self):
        """Calculates the full displacement from the target to the source
           without removing the linear part"""
        fullDisp = mincDisplacement(self.targetFH, self.inputFH, transform=self.invXfm)
        self.p.addStage(fullDisp)
        self.fullDisp = fullDisp.outputFiles[0]
        
    def linAndNlinDisplacement(self):
        """
           The function calculates both the linear and nonlinear
           portions of the displacement, in order to find 
           pure nonlinear. Common space here is the target (usually
           an average of some sort). We also recentre pure non linear 
           displacement. 
           
        """
        
        """Calculate linear part of non-linear xfm from input to target"""
        lpnl = linearPartofNlin(self.inputFH, self.targetFH)
        self.p.addStage(lpnl)
        self.linearXfm = lpnl.outputFiles[0]
        
        """Calculate full displacement from target to input"""
        self.calcFullDisplacement()
        
        """Calculate pure non-linear displacement from target to input
           1. Concatenate linear and inverse target to input transform to 
              get pure_nlin xfm
           2. Compute mincDisplacement on this transform. 
        """
        nlinXfm = createPureNlinXfmName(self.inputFH, self.invXfm)
        xc = xfmConcat([self.linearXfm, self.invXfm], nlinXfm, fh.logFromFile(self.inputFH.logDir, nlinXfm))
        self.p.addStage(xc)
        nlinDisp = mincDisplacement(self.targetFH, self.inputFH, transform=nlinXfm)
        self.p.addStage(nlinDisp)
        self.nlinDisp = nlinDisp.outputFiles[0]
        
        """Calculate average displacement and re-center non-linear displacement
           if an array of input file handlers was specified on instantiation. """
        
        if self.dispToAvg:
            """Calculate average inverse displacement"""
            avgOutput = abspath(self.targetFH.basedir) + "/" + "average_inv_pure_displacement.mnc"
            logBase = fh.removeBaseAndExtension(avgOutput)
            avgLog = fh.createLogFile(self.targetFH.basedir, logBase)
            avg = mincAverageDisp(self.dispToAvg, avgOutput, logFile=avgLog)
            self.p.addStage(avg)
            """Centre pure nlin displacement by subtracting average from existing"""
            centredBase = fh.removeBaseAndExtension(self.nlinDisp).split("_displacement")[0] 
            centredOut = fh.createBaseName(self.inputFH.statsDir, 
                                           centredBase + "_centred_displacement.mnc")
            cmd = ["mincmath", "-clobber", "-sub", InputFile(self.nlinDisp), 
                   InputFile(avgOutput), OutputFile(centredOut)]
            centredDisp = CmdStage(cmd)
            centredDisp.setLogFile(LogFile(fh.logFromFile(self.inputFH.logDir, centredOut)))
            self.p.addStage(centredDisp)
            """Reset centred displacement to be self.nlinDisp"""
            self.nlinDisp = centredOut
             
    def calcDetAndLogDet(self, useFullDisp=False):  
        #Lots of repetition here--let's see if we can't make some functions.
        """useFullDisp indicates whether or not to use full displacement field or non-linear component only""" 
        if useFullDisp:
            dispToUse = self.fullDisp
        else:
            dispToUse = self.nlinDisp
        """Insert -1 at beginning of blurs array to include the calculation of unblurred jacobians."""
        self.blurs.insert(0,-1)    
        for b in self.blurs:
            """Calculate default output filenames and set input for determinant calculation."""
            outputBase = fh.removeBaseAndExtension(dispToUse).split("_displacement")[0]
            inputDet = dispToUse
            outputDet = fh.createBaseName(self.inputFH.tmpDir, outputBase + "_determinant.mnc")
            outDetShift = fh.createBaseName(self.inputFH.tmpDir, outputBase + "_det_plus1.mnc")
            outLogDet = fh.createBaseName(self.inputFH.statsDir, outputBase + "_log_determinant.mnc")
            outLogDetScaled = fh.createBaseName(self.inputFH.statsDir, outputBase + "_log_determinant_scaled.mnc")
            """Calculate smoothed deformation field for all blurs other than -1"""
            if b != -1:
                fwhm = "--fwhm=" + str(b)
                outSmooth = fh.createBaseName(self.inputFH.tmpDir, 
                                       outputBase + "_smooth_displacement_fwhm" + str(b) + ".mnc")
                cmd = ["smooth_vector", "--clobber", "--filter", fwhm, 
                       InputFile(dispToUse), OutputFile(outSmooth)]
                smoothVec = CmdStage(cmd)
                smoothVec.setLogFile(LogFile(fh.logFromFile(self.inputFH.logDir, outSmooth)))
                self.p.addStage(smoothVec)
                """Override file name defaults for each blur and set input for determinant calculation."""
                inputDet = outSmooth
                outputDet = fh.createBaseName(self.inputFH.tmpDir, 
                                          outputBase + "_determinant_fwhm" + str(b) + ".mnc")
                outDetShift = fh.createBaseName(self.inputFH.tmpDir, 
                                          outputBase + "_det_plus1_fwhm" + str(b) + ".mnc")
                outLogDet = fh.createBaseName(self.inputFH.statsDir, 
                                          outputBase + "_log_determinant_fwhm" + str(b) + ".mnc")
                outLogDetScaled = fh.createBaseName(self.inputFH.statsDir, 
                                                    outputBase + "_log_determinant_scaled_fwhm" + str(b) + ".mnc")
            
            """Calculate the determinant, then add 1 (per mincblob weirdness)"""
            
            cmd = ["mincblob", "-clobber", "-determinant", InputFile(inputDet), OutputFile(outputDet)]
            det = CmdStage(cmd)
            det.setLogFile(LogFile(fh.logFromFile(self.inputFH.logDir, outputDet)))
            self.p.addStage(det)
            
            cmd = ["mincmath", "-clobber", "-2", "-const", str(1), "-add", 
                   InputFile(outputDet), OutputFile(outDetShift)]
            det = CmdStage(cmd)
            det.setLogFile(LogFile(fh.logFromFile(self.inputFH.logDir, outDetShift)))
            self.p.addStage(det)
            
            """Calculate log determinant (jacobian) and add to statsGroup."""
            cmd = ["mincmath", "-clobber", "-2", "-log", InputFile(outDetShift), OutputFile(outLogDet)]
            det = CmdStage(cmd)
            det.setLogFile(LogFile(fh.logFromFile(self.inputFH.logDir, outLogDet)))
            self.p.addStage(det)
            self.statsGroup.jacobians[b] = outLogDet
            
            """If self.linearXfm present, calculate scaled log determinant (scaled jacobian) and add to statsGroup"""
            if not useFullDisp:
                """
                    If self.scaleFactor is specified, then concatenate this additional transform
                    with self.linearXfm. Typically, this will come from an LSQ12 registration, but
                    may come from another alignment. 
                """
                if self.scalingFactor:
                    toConcat = [self.scalingFactor, self.linearXfm]
                    self.fullLinearXfm = fh.createBaseName(self.inputFH.transformsDir, self.inputFH.basename + "_full_linear.xfm")
                    logFile=fh.logFromFile(self.inputFH.logDir, fh.removeBaseAndExtension(self.fullLinearXfm))
                    concat = xfmConcat(toConcat, self.fullLinearXfm, logFile=logFile)
                    self.p.addStage(concat)
                else:
                    self.fullLinearXfm = self.linearXfm
                cmd = ["scale_voxels", "-clobber", "-invert", "-log", 
                       InputFile(self.fullLinearXfm), InputFile(outLogDet), OutputFile(outLogDetScaled)]
                det = CmdStage(cmd)
                det.setLogFile(LogFile(fh.logFromFile(self.inputFH.logDir, outLogDetScaled)))
                self.p.addStage(det)
                self.statsGroup.scaledJacobians[b] = outLogDetScaled
            else:
                self.statsGroup.scaledJacobians = None

class CalcChainStats(CalcStats):
    """This class calculates multiple displacement fields, jacobians and scaled jacobians"""
    def __init__(self, inputFH, targetFH, blurs):
        CalcStats.__init__(self, inputFH, targetFH, blurs)
    
    def setupXfms(self):
        self.xfm = self.inputFH.getLastXfm(self.targetFH)
        if not self.xfm:
            print "Cannot calculate statistics. No transform between input and target specified."
            sys.exit()
    
    def calcFullDisplacement(self):
        """Calculates the full displacement from input to target without removing 
           the linear part. Note that inputFH is both source files for displacement
           and location of output and log files. """
        fullDisp = mincDisplacement(self.inputFH, self.inputFH, transform=self.xfm)
        self.p.addStage(fullDisp)
        self.fullDisp = fullDisp.outputFiles[0]
        
    def linAndNlinDisplacement(self):    
        """The function calculates both the linear and nonlinear
           portions of the displacement, in order to find 
           pure nonlinear. Input is the commonSpace, so the pure
           nonlinear displacement will point from input to target.
        
           This is opposite from the standard stats class, where
           the common space is the target
           
        """
        
        """Calculate linear part of non-linear xfm from input to target"""
        lpnl = linearPartofNlin(self.inputFH, self.targetFH)
        self.p.addStage(lpnl)
        self.linearXfm = lpnl.outputFiles[0]
        
        """Invert the transform, so we get the linear xfm from target to input."""
        xi = xfmInvert(self.linearXfm, FH=self.inputFH)
        self.p.addStage(xi)
        
        """Calculate full displacement from input to target"""
        self.calcFullDisplacement()
        
        """Calculate pure non-linear displacement from input to target
           1. Concatenate inverse linear and full input-target xfm to 
              get pure_nlin xfm
           2. Compute mincDisplacement on this transform. 
        """
        nlinBase = fh.removeBaseAndExtension(self.xfm) + "_pure_nlin.xfm"
        nlinXfm = fh.createBaseName(self.inputFH.tmpDir, nlinBase)
        xc = xfmConcat([xi.outputFiles[0], self.xfm], nlinXfm, fh.logFromFile(self.inputFH.logDir, nlinXfm))
        self.p.addStage(xc)
        nlinDisp = mincDisplacement(self.inputFH, self.inputFH, nlinXfm)
        self.p.addStage(nlinDisp)
        self.nlinDisp = nlinDisp.outputFiles[0]
        

class linearPartofNlin(CmdStage):
    def __init__(self, inputFH, targetFH, defaultDir="transforms"):
        CmdStage.__init__(self, None)
        
        try:  
            if isFileHandler(inputFH, targetFH):
                self.inFile = inputFH.getLastBasevol()  
                self.mask = inputFH.getMask()   
                self.xfm = inputFH.getLastXfm(targetFH)     
                self.outfile = self.setOutputFile(inputFH, defaultDir)
                self.logFile = fh.logFromFile(inputFH.logDir, self.outfile)
            else:
                print ("linear part of nlin currently only works using file handlers. "
                       "Exception being raised.")
                raise
    
        except:
            print "Failed in putting together linearPartofNlin command"
            print "Unexpected error: ", sys.exc_info()
            
        self.addDefaults()
        self.finalizeCommand()
        self.setName()
        
    def addDefaults(self):
        self.inputFiles += [self.inFile, self.xfm]   
        self.outputFiles += [self.outfile]       
        self.cmd += ["lin_from_nlin",
                     "-clobber", "-lsq12"] 
        if self.mask: 
            self.inputFiles += [self.mask]
            self.cmd += ["-mask", self.mask]
                 
    def finalizeCommand(self):
        self.cmd += [self.inFile, self.xfm, self.outfile]   
    def setName(self):
        self.name = "lin_from_nlin " 
    def setOutputFile(self, inFile, defaultDir):
        outDir = inFile.setOutputDirectory(defaultDir)
        outBase = (fh.removeBaseAndExtension(self.xfm) + "_linear_part.xfm")
        outputFile = fh.createBaseName(outDir, outBase)
        return(outputFile)  

class mincDisplacement(CmdStage):
    """This class calculates the displacement from an input
       volume, using a specified transform from this input to
       another volume. Must specify input volume, transform from
       that volume to a target, and an outputFH, which is where 
       the output and log files should be stored. The outputFH 
       and inputFH may be the same volume. A default directory
       for the output may optionally be specified, but is tmp if
       unspecified.  
    """
    def __init__(self, inputFH, outputFH, transform, defaultDir="tmp"):
        CmdStage.__init__(self, None)
        try:  
            if isFileHandler(inputFH, outputFH):
                self.inFile = inputFH.getLastBasevol()  
                self.xfm = transform
                self.outfile = self.setOutputFile(outputFH, defaultDir)
                self.logFile = fh.logFromFile(outputFH.logDir, self.outfile)
            else:
                print ("minc_displacement only works using file handlers. "
                       "Exception being raised.")
                raise
    
        except:
            print "Failed in putting together minc_displacement command"
            print "Unexpected error: ", sys.exc_info()
            
        self.addDefaults()
        self.finalizeCommand()
        self.setName()
        
    def addDefaults(self):
        self.inputFiles += [self.inFile, self.xfm]   
        self.outputFiles += [self.outfile]       
        self.cmd += ["minc_displacement", "-clobber"] 
                 
    def finalizeCommand(self):
        self.cmd += [self.inFile, self.xfm, self.outfile]    
    def setName(self):
        self.name = "minc_displacement " 
    def setOutputFile(self, inFile, defaultDir):
        return setDispName(inFile, self.xfm, defaultDir)
    