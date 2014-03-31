#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import division

import numpy as np
import os, sys, time
import PALInferencePTMCMC as ptmcmc
import bayesutils as bu
import scipy.stats as ss

class RJMCMCSampler(object):
    
    """
    Reverse Jump Markov Chain Monte Carlo (RJMCMC) Sampler.
    This implementation is very basic at the moment and 
    simply used the covariance matrix and MAP estimates
    from fixed dimension runs of the various models for 
    trans-dimensional jump proposals. For intra-model jumps
    we just use default PTMCMC jumps (no PT for now).


    @param models: list of model identifiers (used as keys for dictionaries)
    @param pars: list of initial parameter vectors for each model
    @param logp: log prior function (must be normalized for evidence evaluation)
    @param cov: Initial covariance matrix of model parameters for jump proposals
    @param outDir: Full path to output directory for chain files (default = ./chains)

    """


    def __init__(self, models, pars, logls, logps):
        
        # initialize dictionaries
        self.modelDict = {}
        self.samplerDict = {}
        self.TDJumpDict = {}
        self.nmodels = 0


    
    def addSampler(self, model, ndim, logl, logp, cov, loglargs=[], loglkwargs={}, \
                    logpargs=[], logpkwargs={}, comm=MPI.COMM_WORLD, \
                    outDir='./chains', verbose=False):
        """
        Add sampler class from PALInferencePTMCMC.

        @param model: name of model (used in dictionaries to distinguish models)
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
        @param verbose: Update current run-status to the screen (default=False)

        """
        
        # add PTMCMC sampler to sampler dictionary
        self.samplerDict[model] = ptmcmc.PTSampler(ndim, logl, logp, cov, \
                                    loglargs=loglargs, loglkwargs=loglkwargs, \
                                    logpargs=logpargs, logpkwargs=logpkwargs, \
                                    comm=comm, outDir=outDir, verbose=verbose)

        # update model counter
        self.nmodels += 1

    def constructGaussianKDE(self, model, chain):

        """
        Construct a gaussian KDE from a previously run MCMC chain.
        Uses scipy.stats.gaussian_kde

        @param model: name of model
        @param chain: post burn in mcmc chain size nsamples x nparams

        """

        self.TDJumpDict[model] = ss.gaussian_kde(chain.T)



    def gaussianKDEJump(self, x0, m0, iter):

        """
        Uses gaussian KDE to propose jump in new model parameter space.

        @param x0: parameter vector in current model
        @param m0: the current model
        @param iter: iteration of the RJMCMC chain

        @return x1: proposed parameter vector in new model
        @return m1: proposed model
        @return qxy: forward-backward jump probability

        """

        # determine which model to propose jump into
        ind = np.random.randint(0, self.nmodels)
        m1 = self.TDJumpDict.keys()[ind]

        # new parameters
        x1 = self.TDJumpDict[m1].resample(1).flatten()

        # forward-backward jump probability
        p0 = self.TDJumpDict[m0].evaluate(x0)
        p1 = self.TDJumpDict[m0].evaluate(x1)
        qxy = np.log(p0/p1)

        return x1, m1, qxy





        



