#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import division

import numpy as np
import scipy.stats as ss
import os, sys, time

try:
    from mpi4py import MPI
except ImportError:
    import nompi4py as MPI

try:
    import acor
except ImportError:
    print 'Do not have acor package'
    pass


class PTSampler(object):

    """
    Parallel Tempering Markov Chain Monte-Carlo (PTMCMC) sampler. 
    This implementation uses an adaptive jump proposal scheme
    by default using both standard and single component Adaptive
    Metropolis (AM) and Differential Evolution (DE) jumps.

    This implementation also makes use of MPI (mpi4py) to run
    the parallel chains.

    Along with the AM and DE jumps, the user can add custom 
    jump proposals with the ``addProposalToCycle`` fuction. 

    @param ndim: number of dimensions in problem
    @param logl: log-likelihood function
    @param logp: log prior function (must be normalized for evidence evaluation)
    @param cov: Initial covariance matrix of model parameters for jump proposals
    @param loglargs: any additional arguments (apart from the parameter vector) for 
    log likelihood
    @param loglkwargs: any additional keyword arguments (apart from the parameter vector) 
    for log likelihood
    @param logpargs: any additional arguments (apart from the parameter vector) for 
    log like prior
    @param logpkwargs: any additional keyword arguments (apart from the parameter vector) 
    for log prior
    @param outDir: Full path to output directory for chain files (default = ./chains)
    @param verbose: Update current run-status to the screen (default=True)
    @param resume: Resume from a previous chain (still in testing so beware) (default=False)

    """

    def __init__(self, ndim, logl, logp, cov, loglargs=[], loglkwargs={}, \
                 logpargs=[], logpkwargs={}, comm=MPI.COMM_WORLD, \
                 outDir='./chains', verbose=True, resume=False):

        # MPI initialization
        self.comm = comm
        self.MPIrank = self.comm.Get_rank()
        self.nchain = self.comm.Get_size()

        self.ndim = ndim
        self.logl = _function_wrapper(logl, loglargs, loglkwargs)
        self.logp = _function_wrapper(logp, logpargs, logpkwargs)
        self.outDir = outDir
        self.verbose = verbose
        self.resume = resume

        # setup output file
        if not os.path.exists(self.outDir):
            try:
                os.makedirs(self.outDir)
            except OSError:
                pass

        # set up covariance matrix
        self.cov = cov
        self.U, self.S, v = np.linalg.svd(self.cov)
        self.M2 = np.zeros((self.ndim, self.ndim))
        self.mu = np.zeros(self.ndim)

        # initialize proposal cycle
        self.propCycle = []

        # indicator for auxilary jumps
        self.aux = None

        
    def initialize(self, Niter, ladder=None, Tmin=1, Tmax=None, Tskip=100, \
               isave=1000, covUpdate=1000, KDEupdate=10000, SCAMweight=20, \
               AMweight=20, DEweight=20, KDEweight=30, burn=10000, \
               maxIter=None, thin=10, i0=0, neff=100000):
        """
        Initialize MCMC quantities

        @param maxIter: maximum number of iterations
        @Tmin: minumum temperature to use in temperature ladder

        """
        # get maximum number of iteration
        if maxIter is None and self.MPIrank > 0:
            maxIter = 2*Niter
        elif maxIter is None and self.MPIrank == 0:
            maxIter = Niter

        self.ladder = ladder
        self.covUpdate = covUpdate
        self.KDEupdate = KDEupdate
        self.SCAMweight = SCAMweight
        self.AMweight = AMweight
        self.DEweight = DEweight
        self.KDEweight = KDEweight
        self.burn = burn
        self.Tskip = Tskip
        self.thin = thin
        self.isave = isave
        self.Niter = Niter
        self.neff = neff
        self.tstart = 0

        N = int(maxIter/thin)

        self._lnprob = np.zeros(N)
        self._lnlike = np.zeros(N)
        self._chain = np.zeros((N, self.ndim))
        self.naccepted = 0
        self.swapProposed = 0
        self.nswap_accepted = 0

        # set up covariance matrix and DE buffers
        # TODO: better way of allocating this to save memory
        if self.MPIrank == 0:
            self._AMbuffer = np.zeros((self.Niter, self.ndim))
            self._DEbuffer = np.zeros((self.burn, self.ndim))

        ### setup default jump proposal distributions ###

        # add SCAM
        self.addProposalToCycle(self.covarianceJumpProposalSCAM, self.SCAMweight)
        
        # add AM
        self.addProposalToCycle(self.covarianceJumpProposalAM, self.AMweight)
        
        # randomize cycle
        self.randomizeProposalCycle()

        # setup default temperature ladder
        if self.ladder is None:
            self.ladder = self.temperatureLadder(Tmin, Tmax=Tmax)
    
        # temperature for current chain
        self.temp = self.ladder[self.MPIrank]

        # set up output file
        self.fname = self.outDir + '/chain_{0}.txt'.format(self.temp)
        self.resumeLength = 0
        if self.resume and os.path.isfile(self.fname):
            if self.verbose:
                print 'Resuming run from chain file {0}'.format(self.fname)
            try:
                self.resumechain = np.loadtxt(self.fname)
                self.resumeLength = self.resumechain.shape[0]
            except ValueError:
                print 'WARNING: Cant read in file. Removing last line.'
                os.system('sed -ie \'$d\' {0}'.format(self.fname))
                self.resumechain = np.loadtxt(self.fname)
                self.resumeLength = self.resumechain.shape[0]
            self._chainfile = open(self.fname, 'a')
        else:
            self._chainfile = open(self.fname, 'w')
        self._chainfile.close()

    
    def updateChains(self, p0, lnlike0, lnprob0, iter):

        """
        Update chains after jump proposals

        """
        # update buffer
        if self.MPIrank == 0:
            self._AMbuffer[iter,:] = p0

        # put results into arrays
        if iter % self.thin == 0:
            ind = int(iter/self.thin)
            self._chain[ind,:] = p0
            self._lnlike[ind] = lnlike0
            self._lnprob[ind] = lnprob0

        # write to file
        if iter % self.isave == 0 and iter > 1 and iter > self.resumeLength:
            self._writeToFile(iter)

            # write output covariance matrix
            np.save(self.outDir + '/cov.npy', self.cov)
            if self.MPIrank == 0 and self.verbose and iter > 1:
                sys.stdout.write('\r')
                sys.stdout.write('Finished %2.2f percent in %f s Acceptance rate = %g'\
                                 %(iter/self.Niter*100, time.time() - self.tstart, \
                                   self.naccepted/iter))
                sys.stdout.flush()


        

    def sample(self, p0, Niter, ladder=None, Tmin=1, Tmax=None, Tskip=100, \
               isave=1000, covUpdate=1000, KDEupdate=1000, SCAMweight=20, \
               AMweight=20, DEweight=20, KDEweight=30, burn=10000, \
               maxIter=None, thin=10, i0=0, neff=100000):

        """
        Function to carry out PTMCMC sampling.

        @param p0: Initial parameter vector
        @param self.Niter: Number of iterations to use for T = 1 chain
        @param ladder: User defined temperature ladder
        @param Tmin: Minimum temperature in ladder (default=1) 
        @param Tmax: Maximum temperature in ladder (default=None) 
        @param Tskip: Number of steps between proposed temperature swaps (default=100)
        @param isave: Number of iterations before writing to file (default=1000)
        @param covUpdate: Number of iterations between AM covariance updates (default=1000)
        @param KDEUpdate: Number of iterations between KDE updates (default=1000)
        @param SCAMweight: Weight of SCAM jumps in overall jump cycle (default=20)
        @param AMweight: Weight of AM jumps in overall jump cycle (default=20)
        @param DEweight: Weight of DE jumps in overall jump cycle (default=20)
        @param KDEweight: Weight of KDE jumps in overall jump cycle (default=100)
        @param burn: Burn in time (DE jumps added after this iteration) (default=10000)
        @param maxIter: Maximum number of iterations for high temperature chains (default=2*self.Niter)
        @param self.thin: Save every self.thin MCMC samples
        @param i0: Iteration to start MCMC (if i0 !=0, do not re-initialize)
        @param neff: Number of effective samples to collect before terminating

        """

        # get maximum number of iteration
        if maxIter is None and self.MPIrank > 0:
            maxIter = 2*Niter
        elif maxIter is None and self.MPIrank == 0:
            maxIter = Niter

        # set up arrays to store lnprob, lnlike and chain
        N = int(maxIter/thin)
        
        # if picking up from previous run, don't re-initialize
        if i0 == 0:
            self.initialize(Niter, ladder=ladder, Tmin=Tmin, Tmax=Tmax, Tskip=Tskip, \
               isave=isave, covUpdate=covUpdate, KDEupdate=KDEupdate, SCAMweight=SCAMweight, \
               AMweight=AMweight, DEweight=DEweight, KDEweight=KDEweight, burn=burn, \
               maxIter=maxIter, thin=thin, i0=i0, neff=neff)

        ### compute lnprob for initial point in chain ###

        # if resuming, just start with first point in chain
        if self.resume and self.resumeLength > 0:
            p0, lnlike0, lnprob0  = self.resumechain[0,3:], \
                    self.resumechain[0,1], self.resumechain[0,0]
        else:
            # compute prior
            lp = self.logp(p0)

            if lp == float(-np.inf):

                lnprob0 = -np.inf
                lnlike0 = -np.inf

            else:

                lnlike0 = self.logl(p0) 
                lnprob0 = 1/self.temp * lnlike0 + lp

        # record first values
        self.updateChains(p0, lnlike0, lnprob0, i0)

        self.comm.barrier()

        # start iterations
        iter = i0
        self.tstart = time.time()
        runComplete = False
        Neff = 0
        while runComplete == False:
            iter += 1
            accepted = 0
            
            # call PTMCMCOneStep
            p0, lnlike0, lnprob0 = self.PTMCMCOneStep(p0, lnlike0, lnprob0, iter)

            # compute effective number of samples
            if iter % 1000 == 0 and iter > 2*self.burn and self.MPIrank == 0:
                try:
                    Neff = iter/np.max([acor.acor(self._AMbuffer[self.burn:(iter-1),ii])[0] \
                                        for ii in range(self.ndim)])
                    print '\n {0} effective samples'.format(Neff)
                except NameError:
                    Neff = 0
                    pass

            # stop if reached maximum number of iterations
            if self.MPIrank == 0 and iter >= self.Niter-1:
                if self.verbose:
                    print '\nRun Complete'
                runComplete = True
            
            # stop if reached effective number of samples
            if self.MPIrank == 0 and int(Neff) > self.neff:
                if self.verbose:
                    print '\nRun Complete with {0} effective samples'.format(int(Neff))
                runComplete = True

            if self.MPIrank == 0 and runComplete:
                for jj in range(1, self.nchain):
                    self.comm.send(runComplete, dest=jj, tag=55)

            # check for other chains
            if self.MPIrank > 0:
                runComplete = self.comm.Iprobe(source=0, tag=55)
                time.sleep(0.000001) # trick to get around 


    def PTMCMCOneStep(self, p0, lnlike0, lnprob0, iter):

        """
        Function to carry out PTMCMC sampling.

        @param p0: Initial parameter vector
        @param lnlike0: Initial log-likelihood value
        @param lnprob0: Initial log probability value
        @param iter: iteration number

        @return p0: next value of parameter vector after one MCMC step
        @return lnlike0: next value of likelihood after one MCMC step
        @return lnprob0: next value of posterior after one MCMC step

        """ 
        # update covariance matrix
        if (iter-1) % self.covUpdate == 0 and (iter-1) != 0 and self.MPIrank == 0:
            self._updateRecursive(iter-1, self.covUpdate)

            # broadcast to other chains
            [self.comm.send(self.cov, dest=rank+1, tag=111) for rank 
                                            in range(self.nchain-1)]
        
        # check for sent covariance matrix from T = 0 chain
        getCovariance = self.comm.Iprobe(source=0, tag=111)
        time.sleep(0.000001) 

        if getCovariance and self.MPIrank > 0:
            self.cov = self.comm.recv(source=0, tag=111)
            self.U, self.S, v = np.linalg.svd(self.cov)
            getCovariance = 0
        
        # update KDE buffer after burn in
        if (iter-1) % self.KDEupdate == 0 and \
           (iter-1) >= (self.burn+self.KDEupdate) and \
           self.KDEweight > 0 and self.MPIrank == 0:
            self._updateKDE(iter-1)

            if (iter-1) == self.burn + self.KDEupdate:
                self.addProposalToCycle(self.KDEJumpProposal, self.KDEweight)
                self.randomizeProposalCycle()
        

        # update DE buffer
        if (iter-1) % self.burn == 0 and (iter-1) != 0 and self.MPIrank == 0:
            self._updateDEbuffer(iter-1, self.burn)

            # broadcast to other chains
            [self.comm.send(self._DEbuffer, dest=rank+1, tag=222) for rank 
                                                    in range(self.nchain-1)]

        
        # check for sent DE buffer from T = 0 chain
        getDEbuf = self.comm.Iprobe(source=0, tag=222)
        time.sleep(0.000001) 

        if getDEbuf and self.MPIrank > 0:
            self._DEbuffer = self.comm.recv(source=0, tag=222)
            
            # randomize cycle
            if self.DEJump not in self.propCycle:
                self.addProposalToCycle(self.DEJump, self.DEweight)
                self.randomizeProposalCycle()
            
            # reset
            getDEbuf = 0

        # after burn in, add DE jumps
        if (iter-1) == self.burn and self.MPIrank == 0:
            self.addProposalToCycle(self.DEJump, self.DEweight)
            
            # randomize cycle
            self.randomizeProposalCycle()
        
        ### jump proposal ###

        # if resuming, just use previous chain points
        if self.resume and self.resumeLength > 0 and iter < self.resumeLength:
            p0, lnlike0, lnprob0 = self.resumechain[iter,3:], \
                    self.resumechain[iter,1], self.resumechain[iter,0]

            # update acceptance counter
            self.naccepted = iter*self.resumechain[iter,2]
            accepted = 1
        else:
            y, qxy = self._jump(p0, iter)

            # compute prior and likelihood
            lp = self.logp(y)
        
            if lp == -np.inf:

                newlnprob = -np.inf

            else:

                newlnlike = self.logl(y) 
                newlnprob = 1/self.temp * newlnlike + lp

            # hastings step
            diff = newlnprob - lnprob0 + qxy
            if diff > np.log(np.random.rand()):

                # accept jump
                p0, lnlike0, lnprob0 = y, newlnlike, newlnprob

                # update acceptance counter
                self.naccepted += 1
                accepted = 1

        # temperature swap
        swapReturn, p0, lnlike0, lnprob0 = self.PTswap(p0, lnlike0, lnprob0, iter)

        # check return value
        if swapReturn != 0:
            self.swapProposed += 1
            if swapReturn == 2:
                self.nswap_accepted += 1

        self.updateChains(p0, lnlike0, lnprob0, iter)

        return p0, lnlike0, lnprob0

    def PTswap(self, p0, lnlike0, lnprob0, iter):

        """
        Do parallel tempering swap.

        @param p0: current parameter vector
        @param lnlike0: current log-likelihood
        @param lnprob0: current log posterior value
        @param iter: current iteration number

        @return swapReturn: 0 = no swap proposed, 1 = swap proposed and rejected, 2 = swap proposed and accepted
        @return p0: new parameter vector
        @return lnlike0: new log-likelihood
        @return lnprob0: new log posterior value

        """

        # initialize variables 
        readyToSwap = 0
        swapAccepted = 0
        swapProposed = 0

        # if Tskip is reached, block until next chain in ladder is ready for swap proposal
        if iter % self.Tskip == 0 and self.MPIrank < self.nchain-1:
            swapProposed = 1

            # send current likelihood for swap proposal
            self.comm.send(lnlike0, dest=self.MPIrank+1)

            # determine if swap was accepted
            swapAccepted = self.comm.recv(source=self.MPIrank+1)

            # perform swap
            if swapAccepted:

                # exchange likelihood
                lnlike0 = self.comm.recv(source=self.MPIrank+1)

                # exchange parameters
                self.comm.send(p0, dest=self.MPIrank+1)
                p0 = self.comm.recv(source=self.MPIrank+1)

                # calculate new posterior values
                lnprob0 = 1/self.temp * lnlike0 + self.logp(p0)


        # check if next lowest temperature is ready to swap
        elif self.MPIrank > 0:

            readyToSwap = self.comm.Iprobe(source=self.MPIrank-1)
             # trick to get around processor using 100% cpu while waiting
            time.sleep(0.000001) 

            # hotter chain decides acceptance
            if readyToSwap:
                newlnlike = self.comm.recv(source=self.MPIrank-1)
                
                # determine if swap is accepted and tell other chain
                logChainSwap = (1/self.ladder[self.MPIrank-1] - \
                                1/self.ladder[self.MPIrank]) \
                                * (lnlike0 - newlnlike)

                if logChainSwap > np.log(np.random.rand()):
                    swapAccepted = 1
                else:
                    swapAccepted = 0

                # send out result
                self.comm.send(swapAccepted, dest=self.MPIrank-1)

                # perform swap
                if swapAccepted:

                    # exchange likelihood
                    self.comm.send(lnlike0, dest=self.MPIrank-1)
                    lnlike0 = newlnlike

                    # exchange parameters
                    self.comm.send(p0, dest=self.MPIrank-1)
                    p0 = self.comm.recv(source=self.MPIrank-1)
                
                    # calculate new posterior values
                    lnprob0 = 1/self.temp * lnlike0 + self.logp(p0)

        # Return values for colder chain: 0=nothing happened; 1=swap proposed, not accepted; 2=swap proposed & accepted
        if swapProposed:
            if swapAccepted:
                swapReturn = 2
            else:
                swapReturn = 1
        else:
            swapReturn = 0
    
        return swapReturn, p0, lnlike0, lnprob0


    def temperatureLadder(self, Tmin, Tmax=None, tstep=None):

        """
        Method to compute temperature ladder. At the moment this uses
        a geometrically spaced temperature ladder with a temperature
        spacing designed to give 25 % temperature swap acceptance rate.

        """

        #TODO: make options to do other temperature ladders

        if self.nchain > 1:
            if tstep is None and Tmax is None:
                tstep = 1 + np.sqrt(2/self.ndim)
            elif tstep is None and Tmax is not None:
                tstep = np.exp(np.log(Tmax/Tmin)/(self.nchain-1))
            ladder = np.zeros(self.nchain)
            for ii in range(self.nchain): ladder[ii] = Tmin*tstep**ii
        else:
            ladder = np.array([1])

        return ladder


    def _writeToFile(self, iter):

        """
        Function to write chain file. File has 3+ndim columns,
        the first is log-posterior (unweighted), log-likelihood,
        and acceptance probability, followed by parameter values.
        
        @param iter: Iteration of sampler

        """

        self._chainfile = open(self.fname, 'a+')
        for jj in range((iter-self.isave), iter, self.thin):
            ind = int(jj/self.thin)
            pt_acc = 1
            if self.MPIrank < self.nchain-1 and self.swapProposed != 0:
                pt_acc = self.nswap_accepted/self.swapProposed

            self._chainfile.write('%f\t %f\t %f\t %f\t'%(self._lnprob[ind], self._lnlike[ind],\
                                                  self.naccepted/iter, pt_acc))
            self._chainfile.write('\t'.join([str(self._chain[ind,kk]) \
                                            for kk in range(self.ndim)]))
            self._chainfile.write('\n')
        self._chainfile.close()


    # function to update covariance matrix for jump proposals
    def _updateRecursive(self, iter, mem):

        """ 
        Function to recursively update sample covariance matrix.

        @param iter: Iteration of sampler
        @param mem: Number of steps between updates

        """

        it = iter - mem

        if it == 0:
            self.M2 = np.zeros((self.ndim, self.ndim))
            self.mu = np.zeros(self.ndim)

        for ii in range(mem):
            diff = np.zeros(self.ndim)
            it += 1
            for jj in range(self.ndim):
                
                diff[jj] = self._AMbuffer[iter-mem+ii,jj] - self.mu[jj]
                self.mu[jj] += diff[jj]/it

            self.M2 += np.outer(diff, (self._AMbuffer[iter-mem+ii,:]-self.mu))

        self.cov = self.M2/(it-1)  

        # do svd
        self.U, self.S, v = np.linalg.svd(self.cov)

    # function to update gaussian KDE  of posterior based on current samples
    def _updateKDE(self, iter):
        """
        Update gaussian KDE of posterior using previous samples

        @param iter: current iteration of chain

        """
        chain = self._AMbuffer[self.burn:iter,:]
        self.kde = ss.gaussian_kde(chain.T)


    # update DE buffer samples
    def _updateDEbuffer(self, iter, burn):
        """
        Update Differential Evolution with last burn
        values in the total chain

        @param iter: Iteration of sampler
        @param burn: Total number of samples in DE buffer

        """

        self._DEbuffer = self._AMbuffer[iter-burn:iter]



    # KDE jump proposal
    def KDEJumpProposal(self, x, iter, beta):
        """
        Use Gaussian KDE from previous chain values to propose new parameters.
        This does not depend on the current position in parameter space and should 
        help reduce the ACL of the chain.

        @param x: Parameter vector at current position
        @param iter: Iteration of sampler
        @param beta: Inverse temperature of chain

        @return: q: New position in parameter space
        @return: qxy: Forward-Backward jump probability

        """
        # new parameters
        q = self.kde.resample(1).flatten()

        # forward-backward jump probability
        p0 = self.kde.evaluate(x)
        p1 = self.kde.evaluate(q)
        qxy = np.log(p0/p1)

        return q, qxy


        
    # SCAM jump
    def covarianceJumpProposalSCAM(self, x, iter, beta):

        """
        Single Component Adaptive Jump Proposal. This function will occasionally
        jump in more than 1 parameter. It will also occasionally use different
        jump sizes to ensure proper mixing.

        @param x: Parameter vector at current position
        @param iter: Iteration of sampler
        @param beta: Inverse temperature of chain

        @return: q: New position in parameter space
        @return: qxy: Forward-Backward jump probability

        """

        q = x.copy()
        qxy = 0

        # number of parameters to update at once 
        prob = np.random.rand()
        if prob > (1 - 1/self.ndim):
            block = self.ndim

        elif prob > (1 - 2/self.ndim):
            block = np.ceil(self.ndim/2)

        elif prob > 0.8:
            block = 5

        else:
            block = 1

        # adjust step size
        prob = np.random.rand()

        # small jump
        if prob > 0.9:
            scale = 0.2

        # large jump
        elif prob > 0.97:
            scale = 10
        
        # small-medium jump
        #elif prob > 0.6:
            #:wq    scale = 0.5

        # standard medium jump
        else:
            scale = 1.0

        # adjust scale based on temperature
        if self.temp <= 100:
            scale *= np.sqrt(self.temp)

        # get parmeters in new diagonalized basis
        y = np.dot(self.U.T, x)

        # make correlated componentwise adaptive jump
        ind = np.unique(np.random.randint(0, self.ndim, block))
        neff = len(ind)
        cd = 2.4  / np.sqrt(2*neff) * scale 

        y[ind] = y[ind] + np.random.randn(neff) * cd * np.sqrt(self.S[ind])
        q = np.dot(self.U, y)

        return q, qxy
    
    # AM jump
    def covarianceJumpProposalAM(self, x, iter, beta):

        """
        Adaptive Jump Proposal. This function will occasionally 
        use different jump sizes to ensure proper mixing.

        @param x: Parameter vector at current position
        @param iter: Iteration of sampler
        @param beta: Inverse temperature of chain

        @return: q: New position in parameter space
        @return: qxy: Forward-Backward jump probability

        """

        q = x.copy()
        qxy = 0

        # adjust step size
        prob = np.random.rand()

        # small jump
        if prob > 0.9:
            scale = 0.2

        # large jump
        elif prob > 0.97:
            scale = 10
        
        # small-medium jump
        #elif prob > 0.6:
        #    scale = 0.5

        # standard medium jump
        else:
            scale = 1.0

        # adjust scale based on temperature
        if self.temp <= 100:
            scale *= np.sqrt(self.temp)

        cd = 2.4/np.sqrt(2*self.ndim) * np.sqrt(scale)
        q = np.random.multivariate_normal(x, cd**2*self.cov)

        return q, qxy


    # Differential evolution jump
    def DEJump(self, x, iter, beta):

        """
        Differential Evolution Jump. This function will  occasionally 
        use different jump sizes to ensure proper mixing.

        @param x: Parameter vector at current position
        @param iter: Iteration of sampler
        @param beta: Inverse temperature of chain

        @return: q: New position in parameter space
        @return: qxy: Forward-Backward jump probability

        """

        # get old parameters
        q = x.copy()
        qxy = 0

        bufsize = np.alen(self._DEbuffer)

        # draw a random integer from 0 - iter
        mm = np.random.randint(0, bufsize)
        nn = np.random.randint(0, bufsize)

        # make sure mm and nn are not the same iteration
        while mm == nn: nn = np.random.randint(0, bufsize)

        # get jump scale size
        prob = np.random.rand()

        # mode jump
        if prob > 0.5:
            scale = 1.0
            
        else:
            scale = np.random.rand() * 2.4/np.sqrt(2*self.ndim) * np.sqrt(1/beta)

        for ii in range(self.ndim):
            
            # jump size
            sigma = self._DEbuffer[mm, ii] - self._DEbuffer[nn, ii]

            # jump
            q[ii] += scale * sigma
        
        return q, qxy


    # add jump proposal distribution functions
    def addProposalToCycle(self, func, weight):
        """
        Add jump proposal distributions to cycle with a given weight.

        @param func: jump proposal function
        @param weight: jump proposal function weight in cycle

        """

        # get length of cycle so far
        length = len(self.propCycle)

        # check for 0 weight
        if weight == 0:
            print 'ERROR: Can not have 0 weight in proposal cycle!'
            sys.exit()

        # add proposal to cycle
        for ii in range(length, length + weight):
            self.propCycle.append(func)


    # add auxilary jump proposal distribution functions
    def addAuxilaryJump(self, func):
        """
        Add auxilary jump proposal distribution. This will be called after every
        standard jump proposal. Examples include cyclic boundary conditions and 
        pulsar phase fixes

        @param func: jump proposal function

        """
        
        # set auxilary jump
        self.aux = func

    # randomized proposal cycle
    def randomizeProposalCycle(self):
        """
        Randomize proposal cycle that has already been filled

        """

        # get length of full cycle
        length = len(self.propCycle)

        # get random integers
        index = np.random.randint(0, (length-1), length)

        # randomize proposal cycle
        self.randomizedPropCycle = [self.propCycle[ind] for ind in index]


    # call proposal functions from cycle
    def _jump(self, x, iter):
        """
        Call Jump proposals

        """

        # get length of cycle
        length = len(self.propCycle)

        # call function
        q, qxy = self.randomizedPropCycle[np.mod(iter, length)](x, iter, 1/self.temp)

        # axuilary jump
        if self.aux is not None:
            q, qxy_aux = self.aux(x, q, iter, 1/self.temp)
            qxy += qxy_aux

        # increment proposal cycle counter and re-randomize if at end of cycle
        if iter % length == 0: self.randomizeProposalCycle()

        return q, qxy

    # TODO: jump statistics



class _function_wrapper(object):
    """
    This is a hack to make the likelihood function pickleable when ``args``
    or ``kwargs`` are also included.

    """
    def __init__(self, f, args, kwargs):
        self.f = f
        self.args = args
        self.kwargs = kwargs

    def __call__(self, x):
        return self.f(x, *self.args, **self.kwargs)
   








