#!/usr/bin/env python

import networkx as nx
import Pyro
import Queue
import cPickle as pickle
import os
import sys
import socket
import time
from datetime import datetime
from subprocess import call
from shlex import split
from multiprocessing import Process, Event
import file_handling as fh
import pipeline_executor as pe
import logging

logger = logging.getLogger(__name__)

Pyro.config.PYRO_MOBILE_CODE=1 

class PipelineFile():
    def __init__(self, filename):
        self.filename = filename
        self.setType()
    def setType(self):
        self.fileType = None
    def __repr__(self):
        return(self.filename)

class InputFile(PipelineFile):
    def setType(self):
        self.fileType = "input"

class OutputFile(PipelineFile):
    def setType(self):
        self.fileType = "output"

class LogFile(PipelineFile):
    def setType(self):
        self.fileType = "log"

class PipelineStage():
    def __init__(self):
        self.mem = 2.0 # default memory allotted per stage
        self.procs = 1 # default number of processors per stage
        self.inputFiles = [] # the input files for this stage
        self.outputFiles = [] # the output files for this stage
        self.logFile = None # each stage should have only one log file
        self.status = None
        self.name = ""
        self.colour = "black" # used when a graph is created of all stages to colour the nodes

    def isFinished(self):
        if self.status == "finished":
            return True
        else:
            return False
    def setRunning(self):
        self.status = "running"
    def setFinished(self):
        self.status = "finished"
    def setFailed(self):
        self.status = "failed"
    def setNone(self):
        self.status = None
    def setMem(self, mem):
        self.mem = mem
    def getMem(self):
        return self.mem
    def setProcs(self, num):
        self.procs = num
    def getProcs(self):
        return self.procs
    def getHash(self):
        return(hash("".join(self.outputFiles) + "".join(self.inputFiles)))
    def __eq__(self, other):
        if self.inputFiles == other.inputFiles and self.outputFiles == other.outputFiles:
            return True
        else:
            return False
    def __ne__(self, other):
        if self.inputFiles == other.inputFiles and self.outputFiles == other.outputFiles:
            return False
        else:
            return True

class CmdStage(PipelineStage):
    def __init__(self, argArray):
        PipelineStage.__init__(self)
        self.argArray = argArray # the raw input array
        self.cmd = [] # the input array converted to strings
        self.parseArgs()
        self.checkLogFile()
    def parseArgs(self):
        if self.argArray:
            for a in self.argArray:
                ft = getattr(a, "fileType", None)
                if ft == "input":
                    self.inputFiles.append(str(a))
                elif ft == "output":
                    self.outputFiles.append(str(a))
                self.cmd.append(str(a))
                self.name = self.cmd[0]
    def checkLogFile(self):
        if not self.logFile:
            self.logFile = self.name + "." + datetime.isoformat(datetime.now()) + ".log"
    def setLogFile(self, logFileName): 
        self.logFile = str(logFileName)
    def execStage(self):
        of = open(self.logFile, 'w')
        of.write("Running on: " + socket.gethostname() + " at " + datetime.isoformat(datetime.now(), " ") + "\n")
        of.write(repr(self) + "\n")
        of.flush()

        if self.is_effectively_complete():
            of.write("All output files exist. Skipping stage.\n")
            returncode = 0
        else:
            args = split(repr(self)) 
            returncode = call(args, stdout=of, stderr=of, shell=False) 
        of.close()
        return(returncode)
    
    def is_effectively_complete(self):
        """check if this stage is effectively complete (if output files already exist)"""
        all_files_exist = True
        for output in self.outputFiles + self.inputFiles:
            if not os.path.exists(output):
                all_files_exist = False
                break
        return all_files_exist

    def getHash(self):
        return(hash(" ".join(self.cmd)))
    def __repr__(self):
        return(" ".join(self.cmd))

class Pipeline(Pyro.core.SynchronizedObjBase):
    def __init__(self):
        # initialize the remote objects bits
        Pyro.core.SynchronizedObjBase.__init__(self)
        # the core pipeline is stored in a directed graph. The graph is made
        # up of integer indices
        self.G = nx.DiGraph()
        # an array of the actual stages (PipelineStage objects)
        self.stages = []
        self.nameArray = []
        # a queue of the stages ready to be run - contains indices
        self.runnable = Queue.Queue()
        # the current stage counter
        self.counter = 0
        # hash to keep the output to stage association
        self.outputhash = {}
        # a hash per stage - computed from inputs and outputs or whole command
        self.stagehash = {}
        # an array containing the status per stage
        self.processedStages = []
        # location of backup files for restart if needed
        self.backupFileLocation = None
        # list of registered clients
        self.clients = []
        # Initially set number of skipped stages to be 0
        self.skipped_stages = 0
    def addStage(self, stage):
        """adds a stage to the pipeline"""
        # check if stage already exists in pipeline - if so, don't bother

        # check if stage exists - stage uniqueness defined by in- and outputs
        # for base stages and entire command for CmdStages
        h = stage.getHash()
        if self.stagehash.has_key(h):
            self.skipped_stages += 1 
            #stage already exists - nothing to be done
        else: #stage doesn't exist - add it
            # add hash to the dict
            self.stagehash[h] = self.counter
            #self.statusArray[self.counter] = 'notstarted'
            # add the stage itself to the array of stages
            self.stages.append(stage)
            self.nameArray.append(stage.name)
            # add all outputs to the output dictionary
            for o in stage.outputFiles:
                self.outputhash[o] = self.counter
            # add the stage's index to the graph
            self.G.add_node(self.counter, label=stage.name,color=stage.colour)
            # increment the counter for the next stage
            self.counter += 1
    def selfPickle(self):
        """Pickles pipeline in case future restart is needed"""
        if (self.backupFileLocation == None):
            self.setBackupFileLocation()
        pickle.dump(self.G, open(str(self.backupFileLocation) + '/G.pkl', 'wb'))
        pickle.dump(self.stages, open(str(self.backupFileLocation) + '/stages.pkl', 'wb'))
        pickle.dump(self.nameArray, open(str(self.backupFileLocation) + '/nameArray.pkl', 'wb'))
        pickle.dump(self.counter, open(str(self.backupFileLocation) + '/counter.pkl', 'wb'))
        pickle.dump(self.outputhash, open(str(self.backupFileLocation) + '/outputhash.pkl', 'wb'))
        pickle.dump(self.stagehash, open(str(self.backupFileLocation) + '/stagehash.pkl', 'wb'))
        pickle.dump(self.processedStages, open(str(self.backupFileLocation) + '/processedStages.pkl', 'wb'))
        logger.info("Pipeline pickled")
    def restart(self):
        """Restarts the pipeline from previously pickled backup files."""
        if (self.backupFileLocation == None):
            self.setBackupFileLocation()
            logger.info("Backup location not specified. Looking in the current directory.")
        try:
            self.G = pickle.load(open(str(self.backupFileLocation) + '/G.pkl', 'rb'))
            self.stages = pickle.load(open(str(self.backupFileLocation) + '/stages.pkl', 'rb'))
            self.nameArray = pickle.load(open(str(self.backupFileLocation) + '/nameArray.pkl', 'rb'))
            self.counter = pickle.load(open(str(self.backupFileLocation) + '/counter.pkl', 'rb'))
            self.outputhash = pickle.load(open(str(self.backupFileLocation) + '/outputhash.pkl', 'rb'))
            self.stagehash = pickle.load(open(str(self.backupFileLocation) + '/stagehash.pkl', 'rb'))
            self.processedStages = pickle.load(open(str(self.backupFileLocation) + '/processedStages.pkl', 'rb'))
            logger.info('Successfully reimported old data from backups.')
        except:
            logger.exception("Backup files are not recoverable.  Pipeline restart required.")
            sys.exit()

        done = []
        for i in self.G.nodes_iter():
            if self.stages[i].isFinished():
                done.append(i)
            else:
                if i in self.processedStages:
                    self.processedStages.remove(i)
        logger.info('Previously completed stages (of ' + str(len(self.stages)) + ' total): ' + str(len(done)))

    def setBackupFileLocation(self, outputDir=None):
        """Sets location of backup files."""
        if (outputDir == None):
            # set backups in current directory if directory doesn't currently exist
            outputDir = os.getcwd() 
        self.backupFileLocation = fh.createBackupDir(outputDir)   
    def addPipeline(self, p):
        if p.skipped_stages > 0:
            self.skipped_stages += p.skipped_stages
        for s in p.stages:
            self.addStage(s)
    def printStages(self, name):
        """Prints stages to a file, stage info to stdout"""
        fileForPrinting = os.path.abspath(os.curdir + "/" + str(name) + "-pipeline-stages.txt")
        pf = open(fileForPrinting, "w")
        for i in range(len(self.stages)):
            pf.write(str(i) + "  " + str(self.stages[i]) + "\n")
        pf.close()
        print self.skipped_stages, "stages skipped as redundant. ", len(self.stages), "stages to run."
                   
    def createEdges(self):
        """computes stage dependencies by examining their inputs/outputs"""
        starttime = time.time()
        # iterate over all nodes
        for i in self.G.nodes_iter():
            for ip in self.stages[i].inputFiles:
                # if the input to the current stage was the output of another
                # stage, add a directional dependence to the DiGraph
                if self.outputhash.has_key(ip):
                    self.G.add_edge(self.outputhash[ip], i)
        endtime = time.time()
        logger.info("Create Edges time: " + str(endtime-starttime))
    def computeGraphHeads(self):
        """adds stages with no incomplete predecessors to the runnable queue"""
        graphHeads = []
        for i in self.G.nodes_iter():
            if self.stages[i].isFinished() == False:
                """ either it has 0 predecessors """
                if len(self.G.predecessors(i)) == 0:
                    self.runnable.put(i)
                    graphHeads.append(i)
                """ or all of its predecessors are finished """
                if len(self.G.predecessors(i)) != 0:
                    predfinished = True
                    for j in self.G.predecessors(i):
                        if self.stages[j].isFinished() == False:
                            predfinished = False
                    if predfinished == True:
                        self.runnable.put(i) 
                        graphHeads.append(i)
        logger.info("Graph heads: " + str(graphHeads))
    def getStage(self, i):
        """given an index, return the actual pipelineStage object"""
        return(self.stages[i])
    def getRunnableStageIndex(self):
        """returns the next runnable stage, or None"""
        if self.runnable.empty():
            return None
        else:
            index = self.runnable.get()
            self.stages[index].setRunning()
            return index
        
    def setStageStarted(self, index, clientURI=None):
        URIstring = " "
        if clientURI:
            URIstring = "(" + str(clientURI) + ")"
        logger.debug("Starting Stage " + str(index) + ": " + str(self.stages[index]) +
                     URIstring)

    def checkIfRunnable(self, index):
        """stage added to runnable queue if all predecessors finished"""
        canRun = True
        logger.debug("Checking if stage " + str(index) + " is runnable ...")
        if self.stages[index].isFinished() == True:
            canRun = False
        else:
            for i in self.G.predecessors(index):              
                s = self.getStage(i)
                if s.isFinished() == False:
                    canRun = False
        logger.debug("Stage " + str(index) + " Runnable: " + str(canRun))
        return canRun

    def setStageFinished(self, index, save_state = True):
        """given an index, sets corresponding stage to finished and adds successors to the runnable queue"""
        logger.info("Finished Stage " + str(index) + ": " + str(self.stages[index]))
        self.stages[index].setFinished()
        self.processedStages.append(index)
        if save_state: 
            self.selfPickle()
        for i in self.G.successors(index):
            if self.checkIfRunnable(i):
                self.runnable.put(i)

    def setStageFailed(self, index):
        """given an index, sets stage to failed, adds to processed stages array"""
        self.stages[index].setFailed()
        logger.info("ERROR in Stage " + str(index) + ": " + str(self.stages[index]))
        self.processedStages.append(index)
        for i in nx.dfs_successor(self.G, index).keys():
            self.processedStages.append(i)

    def requeue(self, i):
        """If stage cannot be run due to insufficient mem/procs, executor returns it to the queue"""
        self.stages[i].setNone()
        self.runnable.put(i)            
    def initialize(self):
        """called once all stages have been added - computes dependencies and adds graph heads to runnable queue"""
        self.runnable = Queue.Queue()
        self.createEdges()
        self.computeGraphHeads()
    def continueLoop(self):
        """Returns 1 unless all stages are finished. Used in Pyro communication."""
        return(len(self.stages) > len(self.processedStages))
    def getProcessedStageCount(self):
        return(len(self.processedStages))
    def register(self, client):
        """Adds new client to array of registered clients."""
        print "CLIENT REGISTERED: " + str(client)
        self.clients.append(client)

def launchPipelineExecutor(options, programName=None):
    """Launch pipeline executor directly from pipeline"""
    pipelineExecutor = pe.pipelineExecutor(options)
    if options.queue=="sge":
        pipelineExecutor.submitToQueue(programName) 
    else: 
        pipelineExecutor.launchExecutor()    

def skip_completed_stages(pipeline):
    runnable = []
    while True:
        i = pipeline.getRunnableStageIndex()                
        if i == None:
            break
        
        s = pipeline.getStage(i)
        if not isinstance(s, CmdStage):
            runnable.append(i)
            continue
        
        if not s.is_effectively_complete():
            runnable.append(i)
            continue
        
        pipeline.setStageStarted(i, "PYRO://Previous.Run")
        pipeline.setStageFinished(i, save_state = False)
        logger.debug("skipping stage %i" % i)
    
    for i in runnable:
        pipeline.requeue(i)
        
def launchServer(pipeline, options, e):
    """Starts Pyro Server in a separate thread"""
    Pyro.core.initServer()
    daemon=Pyro.core.Daemon()
    
    #Note: Pyro NameServer must be started for this function to work properly
    if options.use_ns:
        ns=Pyro.naming.NameServerLocator().getNS()
        daemon.useNameServer(ns)
    
    uri=daemon.connect(pipeline, "pipeline")
    print("Daemon is running on port: " + str(daemon.port))
    print("The object's uri is: " + str(uri))

    # If not using Pyro NameServer, must write uri to file for reading by client.
    if not options.use_ns:
        uf = open(options.urifile, 'w')
        uf.write(str(uri))
        uf.close()
    
    e.set()
    
    try:
        daemon.requestLoop(pipeline.continueLoop) 
    except:
        logger.exception("Failed running server in daemon.requestLoop. Server shutting down.")
    else:
        try:
            print("All pipeline stages have been processed. Daemon unregistering " 
                  + str(len(pipeline.clients)) + " client(s) and shutting down...")
            sys.stdout.flush()
            for c in pipeline.clients[:]:
                clientObj = Pyro.core.getProxyForURI(c)
                clientObj.serverShutdownCall()
                print "Made serverShutdownCall to: " + str(c)
                pipeline.clients.remove(c)
                print "Client deregistered from server: "  + str(c)
                sys.stdout.flush()
            daemon.shutdown(True)
            print("Objects successfully unregistered and daemon shutdown.")
        except:
            logger.exception("Failed to successfully de-register all clients")

def flatten_pipeline(p):
    """return a list of tuples for each stage.
       Each item in the list is (id, command, [dependencies]) 
       where dependencies is a list of stages depend on this stage to be complete before they run.
    """
    def post(x, y):
        if y[0] in x[2]: 
            return 1
        elif x[0] in y[2]:
            return -1
        else:
            return 0 
                
    return sorted([(i, str(p.stages[i]), p.G.predecessors(i)) for i in p.G.nodes_iter()],cmp=post)

def sge_script(p):
    qsub = "sge_batch_hold -l vf=2G"
    flat = flatten_pipeline(p)

    subs   = []
    alter  = []
    unhold = []
    f = lambda x: "MAGeT_%i" % x

    script = []
    skipped_stages = 0
    for i in flat:
        job_id,cmd,depends = i
        stage  = p.getStage(job_id)
        if isinstance(stage, CmdStage): 
            if stage.is_effectively_complete():
                skipped_stages += 1
                continue
        name = f(job_id)
        deps = ",".join(map(f,depends))
        job_cmd="%s -J %s %s" % (qsub, name, cmd)
        script.append(job_cmd)
        if depends:
            depend_cmd="qalter -hold_jid %s %s" % (deps,name)
            script.append(depend_cmd)
        unhold_cmd = "qalter -h U %s" % name
        script.append(unhold_cmd)
    
    print skipped_stages, "stages skipped (outputs exist).", len(subs), "stages to run."
    return script #subs + alter + unhold

def pipelineDaemon(pipeline, options=None, programName=None):
    """Launches Pyro server and (if specified by options) pipeline executors"""

    #check for valid pipeline 
    if pipeline.runnable.empty()==None:
        print "Pipeline has no runnable stages. Exiting..."
        sys.exit()

    if options.queue == "sge_script":
        script = open("sge_script", "w")
        script.write("\n".join(sge_script(pipeline)))
        script.close()
        print "SGE job submission script for this pipeline written to sge_script"
        sys.exit()

    if options.urifile==None:
        options.urifile = os.path.abspath(os.curdir + "/" + "uri")
        
    logger.debug("Examining filesystem to determine skippable stages...")
    skip_completed_stages(pipeline)
    
    e = Event()
    logger.debug("Prior to starting server, total stages %i. Number processed: %i.", 
                 len(pipeline.stages), len(pipeline.processedStages))
    logger.debug("Number of stages in runnable index (size of queue): %i",
                 pipeline.runnable.qsize())
    logger.debug("Starting server...")
    process = Process(target=launchServer, args=(pipeline,options,e,))
    process.start()
    e.wait()
    
    if options.num_exec != 0 and pipeline.runnable.qsize() > 0:
        try:
            logger.debug("Launching executors...")
            processes = [Process(target=launchPipelineExecutor, args=(options,programName,)) for i in range(options.num_exec)]
            for p in processes:
                p.start()
                    
        except:
            logger.exception("Failed when pipeline called and ran its own executors.")

    process.join()