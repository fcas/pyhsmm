import numpy as np
from numpy import newaxis as na
from numpy.random import random
import scipy.weave

from ..util.stats import sample_discrete, sample_discrete_from_log
from ..util import general as util # perhaps a confusing name :P


class HMMStatesPython(object):
    def __init__(self,model,T=None,data=None,stateseq=None,initialize_from_prior=True):
        self.model = model

        assert (data is None) ^ (T is None)
        self.T = data.shape[0] if data is not None else T
        self.data = data

        if stateseq is not None:
            self.stateseq = np.array(stateseq,dtype=np.int32)
        else:
            if data is not None and not initialize_from_prior:
                self.resample()
            else:
                self.generate_states()

    ### generation

    def generate(self):
        self.generate_states()
        return self.generate_obs()

    def generate_states(self):
        T = self.T
        nextstate_distn = self.model.init_state_distn.pi_0
        A = self.model.trans_distn.A

        stateseq = np.zeros(T,dtype=np.int32)
        for idx in xrange(T):
            stateseq[idx] = sample_discrete(nextstate_distn)
            nextstate_distn = A[stateseq[idx]]

        self.stateseq = stateseq
        return stateseq

    def generate_obs(self):
        obs = []
        for state,dur in zip(*util.rle(self.stateseq)):
            obs.append(self.model.obs_distns[state].rvs(size=int(dur)))
        return np.concatenate(obs)

    ### message passing

    def get_aBl(self,data):
        aBl = np.empty((data.shape[0],self.model.state_dim))
        for idx, obs_distn in enumerate(self.model.obs_distns):
            aBl[:,idx] = obs_distn.log_likelihood(data)
        return aBl

    def messages_forwards(self,aBl):
        # note: this method never uses self.T
        # or self.data
        T = aBl.shape[0]
        alphal = np.zeros((T,self.model.state_dim))
        Al = np.log(self.model.trans_distn.A)

        alphal[0] = np.log(self.model.init_state_distn.pi_0) + aBl[0]

        for t in xrange(T-1):
            alphal[t+1] = np.logaddexp.reduce(alphal[t] + Al.T,axis=1) + aBl[t+1]

        return alphal

    def messages_backwards(self,aBl):
        betal = np.zeros(aBl.shape)
        Al = np.log(self.model.trans_distn.A)
        T = aBl.shape[0]

        for t in xrange(T-2,-1,-1):
            np.logaddexp.reduce(Al + betal[t+1] + aBl[t+1],axis=1,out=betal[t])

        return betal

    ### Gibbs sampling

    def sample_forwards(self,aBl,betal):
        T = aBl.shape[0]
        stateseq = np.zeros(T,dtype=np.int32)
        nextstate_unsmoothed = self.model.init_state_distn.pi_0
        A = self.model.trans_distn.A

        for idx in xrange(T):
            logdomain = betal[idx] + aBl[idx]
            logdomain[nextstate_unsmoothed == 0] = -np.inf # to enforce constraints in the trans matrix
            stateseq[idx] = sample_discrete(nextstate_unsmoothed * np.exp(logdomain - np.amax(logdomain)))
            nextstate_unsmoothed = A[stateseq[idx]]

        self.stateseq = stateseq

    def resample(self):
        assert self.data is not None
        data = self.data

        aBl = self.get_aBl(data)
        betal = self.messages_backwards(aBl)
        self.sample_forwards(aBl,betal)

        return self

    ### EM

    def E_step(self):
        aBl = self.aBl = self.get_aBl(self.data) # saving aBl makes transition distn job easier

        alphal = self.alphal = self.messages_forwards(aBl)
        betal = self.betal = self.messages_backwards(aBl)
        expectations = self.expectations = alphal + betal

        expectations -= expectations.max(1)[:,na]
        np.exp(expectations,out=expectations)
        expectations /= expectations.sum(1)[:,na]

    ### plotting

    def plot(self,colors_dict=None,vertical_extent=(0,1),**kwargs):
        from matplotlib import pyplot as plt
        states,durations = util.rle(self.stateseq)
        X,Y = np.meshgrid(np.hstack((0,durations.cumsum())),vertical_extent)

        if colors_dict is not None:
            C = np.array([[colors_dict[state] for state in states]])
        else:
            C = states[na,:]

        plt.pcolor(X,Y,C,vmin=0,vmax=1,**kwargs)
        plt.ylim(vertical_extent)
        plt.xlim((0,durations.sum()))
        plt.yticks([])

class HMMStatesEigen(HMMStatesPython):
    def __init__(self,model,*args,**kwargs):
        super(HMMStatesEigen,self).__init__(model,*args,**kwargs)

    def messages_backwards(self,aBl):
        global hmm_messages_backwards_codestr, eigen_path

        T = self.T
        M = self.model.state_dim
        AT = self.model.trans_distn.A.T.copy()
        betal = np.zeros((T,M))

        scipy.weave.inline(hmm_messages_backwards_codestr,['AT','betal','aBl','T','M'],
                headers=['<Eigen/Core>'],include_dirs=[eigen_path],
                extra_compile_args=['-O3','-DNDEBUG'])

        return betal

    def sample_forwards(self,aBl,betal):
        global hmm_sample_forwards_codestr, eigen_path

        T = self.T
        M = self.model.state_dim
        A = self.model.trans_distn.A
        pi0 = self.model.init_state_distn.pi_0

        stateseq = np.zeros(T,dtype=np.int32)

        scipy.weave.inline(hmm_sample_forwards_codestr,['A','T','pi0','stateseq','aBl','betal','M'],
                headers=['<Eigen/Core>','<limits>'],include_dirs=[eigen_path],
                extra_compile_args=['-O3','-DNDEBUG'])

        self.stateseq = stateseq


class HSMMStatesPython(HMMStatesPython):
    '''
    HSMM states distribution class. Connects the whole model.

    Parameters include:

    T
    state_dim
    obs_distns
    dur_distns
    transition_distn
    initial_distn
    trunc

    stateseq
    durations
    stateseq_norep
    '''

    def __init__(self,model,censoring=True,trunc=None,*args,**kwargs):
        self.censoring = censoring
        self.trunc = trunc
        super(HSMMStatesPython,self).__init__(model,*args,**kwargs)
        self.stateseq_norep, self.durations = util.rle(self.stateseq)

    ### generation

    def generate_states(self):
        idx = 0
        nextstate_distr = self.model.init_state_distn.pi_0
        A = self.model.trans_distn.A

        stateseq = np.empty(self.T,dtype=np.int32)
        durations = []

        while idx < self.T:
            # sample a state
            state = sample_discrete(nextstate_distr)
            # sample a duration for that state
            duration = self.model.dur_distns[state].rvs()
            # save everything
            durations.append(duration)
            stateseq[idx:idx+duration] = state # this can run off the end, that's okay
            # set up next state distribution
            nextstate_distr = A[state,]
            # update index
            idx += duration

        self.stateseq = stateseq
        self.stateseq_norep, _ = util.rle(stateseq)
        self.durations = np.array(durations,dtype=np.int32) # sum(self.durations) >= self.T

    ### message passing

    def messages_backwards(self,Al,aDl,aDsl,trunc):
        T = aDl.shape[0]
        trunc = trunc if trunc is not None else T
        state_dim = Al.shape[0]
        betal = np.zeros((T,state_dim),dtype=np.float64)
        betastarl = np.zeros((T,state_dim),dtype=np.float64)

        for t in xrange(T-1,-1,-1):
            np.logaddexp.reduce(betal[t:t+trunc] + self.cumulative_likelihoods(t,t+trunc) + aDl[:min(trunc,T-t)],axis=0, out=betastarl[t])
            if T-t < trunc and self.censoring:
                np.logaddexp(betastarl[t], self.likelihood_block(t,None) + aDsl[T-t -1], betastarl[t])
            np.logaddexp.reduce(betastarl[t] + Al,axis=1,out=betal[t-1])
        betal[-1] = 0.

        return betal, betastarl

    def cumulative_likelihoods(self,start,stop):
        return np.cumsum(self.aBl[start:stop],axis=0)

    def cumulative_likelihood_state(self,start,stop,state):
        return np.cumsum(self.aBl[start:stop,state])

    def likelihood_block(self,start,stop):
        # does not include the stop index
        return np.sum(self.aBl[start:stop],axis=0)

    def likelihood_block_state(self,start,stop,state):
        return np.sum(self.aBl[start:stop,state])

    ### Gibbs sampling

    def resample(self):
        assert self.data is not None
        data = self.data

        Al = np.log(self.model.trans_distn.A)
        T, state_dim = self.T, self.model.state_dim
        trunc = self.trunc = self.trunc if self.trunc is not None else T

        # TODO move all the setup work into message passing function, then have
        # a helper that might be static

        # generate and cache likelihood values, used in cumulative_likelihood functions
        self.aBl = self.get_aBl(data)

        # generate duration pmf and sf values
        aDl = np.zeros((T,state_dim))
        aDsl = np.zeros((T,state_dim))
        possible_durations = np.arange(1,T + 1,dtype=np.float64)
        for idx, dur_distn in enumerate(self.model.dur_distns):
            aDl[:,idx] = dur_distn.log_pmf(possible_durations)
            aDsl[:,idx] = dur_distn.log_sf(possible_durations)

        # run backwards message passing
        betal, betastarl = self.messages_backwards(Al,aDl,aDsl,trunc)

        # sample forwards
        self.sample_forwards(betal,betastarl)

        return self

    def sample_forwards(self,betal,betastarl):
        T, state_dim = self.T, self.model.state_dim
        stateseq = self.stateseq = np.zeros(T,dtype=np.int32)
        durations = []
        stateseq_norep = []

        idx = 0
        A = self.model.trans_distn.A
        nextstate_unsmoothed = self.model.init_state_distn.pi_0

        apmf = np.zeros((state_dim,T))
        arg = np.arange(1,T+1)
        for state_idx, dur_distn in enumerate(self.model.dur_distns):
            apmf[state_idx] = dur_distn.pmf(arg)

        while idx < T:
            logdomain = betastarl[idx] - np.amax(betastarl[idx])
            nextstate_distr = np.exp(logdomain) * nextstate_unsmoothed
            if (nextstate_distr == 0.).all():
                # this is a numerical issue; no good answer, so we'll just follow the messages.
                nextstate_distr = np.exp(logdomain)
            state = sample_discrete(nextstate_distr)
            assert len(stateseq_norep) == 0 or state != stateseq_norep[-1]

            durprob = random()
            dur = 0 # always incremented at least once
            prob_so_far = 0.0
            while durprob > 0:
                assert dur < 2*T # hacky infinite loop check
                #assert self.model.dur_distns[state].pmf(dur+1) == apmf[state,dur]
                # NOTE: funny indexing: dur variable is 1 less than actual dur we're considering
                p_d_marg = apmf[state,dur] if dur < T else 1.
                assert not np.isnan(p_d_marg)
                assert p_d_marg >= 0
                if p_d_marg == 0:
                    dur += 1
                    continue
                if idx+dur < T:
                    mess_term = np.exp(self.likelihood_block_state(idx,idx+dur+1,state) \
                            + betal[idx+dur,state] - betastarl[idx,state])
                    p_d = mess_term * p_d_marg
                    prob_so_far += p_d

                    assert not np.isnan(p_d)
                    durprob -= p_d
                    dur += 1
                else:
                    if self.censoring:
                        # TODO 3*T is a hacky approximation, could use log_sf
                        dur += sample_discrete(dur_distn.pmf(np.arange(dur+1,3*self.T)))
                    else:
                        dur += 1
                    durprob = -1 # just to get us out of loop

            assert dur > 0

            stateseq[idx:idx+dur] = state
            stateseq_norep.append(state)
            assert len(stateseq_norep) < 2 or stateseq_norep[-1] != stateseq_norep[-2]
            durations.append(dur)

            nextstate_unsmoothed = A[state,:]

            idx += dur

        self.durations = np.array(durations,dtype=np.int32)
        self.stateseq_norep = np.array(stateseq_norep,dtype=np.int32)

    def plot(self,colors_dict=None,**kwargs):
        from matplotlib import pyplot as plt
        X,Y = np.meshgrid(np.hstack((0,self.durations.cumsum())),(0,1))

        if colors_dict is not None:
            C = np.array([[colors_dict[state] for state in self.stateseq_norep]])
        else:
            C = self.stateseq_norep[na,:]

        plt.pcolor(X,Y,C,vmin=0,vmax=1,**kwargs)
        plt.ylim((0,1))
        plt.xlim((0,self.T))
        plt.yticks([])
        plt.title('State Sequence')


class HSMMStatesEigen(HSMMStatesPython):
    def sample_forwards(self,betal,betastarl):
        global hsmm_sample_forwards_codestr, eigen_path

        aBl = self.aBl
        stateseq = np.zeros(betal.shape[0],dtype=np.int32)
        A = self.model.trans_distn.A
        pi0 = self.model.init_state_distn.pi_0
        M = self.model.state_dim
        T = self.T

        apmf = np.zeros((M,T))
        arg = np.arange(1,T+1)
        for state_idx, dur_distn in enumerate(self.model.dur_distns):
            apmf[state_idx] = dur_distn.pmf(arg)

        scipy.weave.inline(hsmm_sample_forwards_codestr,
                ['betal','betastarl','aBl','stateseq','A','pi0','apmf','M','T'],
                headers=['<Eigen/Core>'],include_dirs=['/usr/local/include/eigen3'],
                extra_compile_args=['-O3','-DNDEBUG'])

        self.stateseq_norep, self.durations = util.rle(stateseq)
        self.stateseq = stateseq

        if self.censoring:
            dur = self.durations[-1]
            dur_distn = self.model.dur_distns[self.stateseq_norep[-1]]
            # TODO instead of 3*T, use log_sf
            self.durations[-1] += sample_discrete(dur_distn.pmf(np.arange(dur+1,3*self.T)))

class HSMMStatesPossibleChangepoints(HSMMStatesPython):
    def __init__(self,model,changepoints,*args,**kwargs):
        self.changepoints = changepoints
        self.startpoints = np.array([start for start,stop in changepoints],dtype=np.int32)
        self.blocklens = np.array([stop-start for start,stop in changepoints],dtype=np.int32)
        self.Tblock = len(changepoints) # number of blocks
        super(HSMMStatesPossibleChangepoints,self).__init__(model,*args,**kwargs)

    def messages_backwards(self,Al,aDl,aDsl,trunc):
        Tblock = self.Tblock
        state_dim = Al.shape[0]
        betal = np.zeros((Tblock,state_dim),dtype=np.float64)
        betastarl = np.zeros(betal.shape)
        trunc = trunc if trunc is not None else self.T

        for tblock in range(Tblock-1,-1,-1):
            possible_durations = self.blocklens[tblock:].cumsum() # TODO could precompute these
            possible_durations = possible_durations[possible_durations < max(trunc,possible_durations[0]+1)]
            truncblock = len(possible_durations)
            normalizer = np.logaddexp.reduce(aDl[possible_durations-1],axis=0)

            np.logaddexp.reduce(betal[tblock:tblock+truncblock]
                    + self.block_cumulative_likelihoods(tblock,tblock+truncblock,possible_durations)
                    + aDl[possible_durations-1] - normalizer,axis=0,out=betastarl[tblock])
            # TODO TODO put censoring here, must implement likelihood_block
            np.logaddexp.reduce(betastarl[tblock] + Al, axis=1, out=betal[tblock-1])
        betal[-1] = 0.

        assert not np.isnan(betal).any()
        assert not np.isnan(betastarl).any()

        # TODO a little ugly: set self.aDl so that this class's sample_forwards() can
        # get it even though the parent class's resample() doesn't pass it in
        self.aDl = aDl

        return betal, betastarl

    def get_aBl(self,data):
        # this method also sets self.aBBl for block likelihoods
        aBBl = np.zeros((self.Tblock,self.model.state_dim))
        aBl = super(HSMMStatesPossibleChangepoints,self).get_aBl(data)
        for idx, (start,stop) in enumerate(self.changepoints):
            aBBl[idx] = aBl[start:stop].sum(0)
        self.aBBl = aBBl
        return None

    def block_cumulative_likelihoods(self,startblock,stopblock,possible_durations):
        return self.aBBl[startblock:stopblock].cumsum(0)[:possible_durations.shape[0]]

    def block_cumulative_likelihood_state(self,startblock,stopblock,state,possible_durations):
        return self.aBBl[startblock:stopblock,state].cumsum(0)[:possible_durations.shape[0]]

    def sample_forwards(self,betal,betastarl):
        aDl = self.aDl
        trunc = self.trunc

        Tblock = betal.shape[0]
        assert Tblock == len(self.changepoints)
        blockstateseq = np.zeros(Tblock,dtype=np.int32)

        tblock = 0
        nextstate_unsmoothed = self.model.init_state_distn.pi_0
        A = self.model.trans_distn.A
        trunc = trunc if trunc is not None else self.T

        while tblock < Tblock:
            # sample the state
            logdomain = betastarl[tblock] - np.amax(betastarl[tblock])
            nextstate_distr = np.exp(logdomain) * nextstate_unsmoothed
            state = sample_discrete(nextstate_distr)

            # compute possible duration info (indep. of state)
            # TODO TODO doesn't handle censoring quite correctly
            possible_durations = self.blocklens[tblock:].cumsum()
            possible_durations = possible_durations[possible_durations < max(trunc,possible_durations[0]+1)]
            truncblock = len(possible_durations)

            if truncblock > 1:
                # compute the next few log likelihoods
                loglikelihoods = self.block_cumulative_likelihood_state(tblock,tblock+truncblock,state,possible_durations)

                # compute pmf over those steps
                logpmf = aDl[possible_durations-1,state] + loglikelihoods + betal[tblock:tblock+truncblock,state] - betastarl[tblock,state]

                # sample from it
                blockdur = sample_discrete_from_log(logpmf)+1
            else:
                blockdur = 1

            # set block sequence
            blockstateseq[tblock:tblock+blockdur] = state

            # set up next iteration
            tblock += blockdur
            nextstate_unsmoothed = A[state]

        # convert block state sequence to full stateseq and stateseq_norep and
        # durations
        self.stateseq = np.zeros(self.T,dtype=np.int32)
        for state, (start,stop) in zip(blockstateseq,self.changepoints):
            self.stateseq[start:stop] = state
        self.stateseq_norep, self.durations = util.rle(self.stateseq)

        # clean up the data passed to us from this class's messages_forwards()
        del self.aDl

    def generate_states(self):
        # this method can probably call sample_forwards with dummy uniform
        # aBl/betal/betastarl, but that's just too complicated!
        Tblock = self.Tblock
        assert Tblock == len(self.changepoints)
        blockstateseq = np.zeros(Tblock,dtype=np.int32)

        tblock = 0
        nextstate_distr = self.model.init_state_distn.pi_0
        A = self.model.trans_distn.A

        while tblock < Tblock:
            # sample the state
            state = sample_discrete(nextstate_distr)

            # compute possible duration info (indep. of state)
            possible_durations = self.blocklens[tblock:].cumsum()

            # compute the pmf over those steps
            durprobs = self.model.dur_distns[state].pmf(possible_durations)
            # TODO censoring: the last possible duration isn't quite right
            durprobs /= durprobs.sum()

            # sample it
            blockdur = sample_discrete(durprobs) + 1

            # set block sequence
            blockstateseq[tblock:tblock+blockdur] = state

            # set up next iteration
            tblock += blockdur
            nextstate_distr = A[state]

        # convert block state sequence to full stateseq and stateseq_norep and
        # durations
        self.stateseq = np.zeros(self.T,dtype=np.int32)
        for state, (start,stop) in zip(blockstateseq,self.changepoints):
            self.stateseq[start:stop] = state
        self.stateseq_norep, self.durations = util.rle(self.stateseq)

        return self.stateseq

    def generate(self): # TODO
        raise NotImplementedError

class HSMMStatesGeoApproximation(HSMMStatesPython):
    def __init__(self,*args,**kwargs):
        super(HSMMStatesGeoApproximation,self).__init__(*args,**kwargs)
        self._hmm_states = HMMStates(model=self.model,data=self.data,stateseq=self.stateseq)

    def messages_backwards(self,Al,aDl,aDsl,trunc):
        'approximates duration tails at indices > trunc with geometric tails'
        assert trunc > 0
        assert trunc > 1 # if trunc == 1, aDsl won't work, just need sf(trunc-1) == 1

        ### run HMM message passing for tail approximation

        hmm_A = self.model.trans_distn.A.copy()
        hmm_A.flat[::self.state_dim+1] = 0
        thediag = np.array([np.exp(d.log_pmf(trunc+1)-d.log_pmf(trunc))[0] for d in self.model.dur_distns])
        hmm_A *= ((1-thediag)/hmm_A.sum(1))[:,na]
        hmm_A.flat[::self.state_dim+1] = thediag
        self._hmm_states.transition_distn.A = hmm_A
        hmm_betal = self._hmm_states.messages_backwards(self.aBl)

        ### run HSMM message passing almost as before

        T = aDl.shape[0]
        state_dim = Al.shape[0]
        betal = np.zeros((T,state_dim),dtype=np.float64)
        betastarl = np.zeros((T,state_dim),dtype=np.float64)

        for t in xrange(T-1,-1,-1):
            np.logaddexp.reduce(betal[t:t+trunc] + self.cumulative_likelihoods(t,t+trunc) + aDl[:min(trunc,T-t)],axis=0, out=betastarl[t])
            # geometric approximation term using HMM messages
            if t+trunc < T:
                np.logaddexp(betastarl[t], self.likelihood_block(t,t+trunc+1) + aDsl[trunc -1] + hmm_betal[t+trunc], out=betastarl[t])
            if T-t < trunc and self.censoring:
                np.logaddexp(betastarl[t], self.likelihood_block(t,None) + aDsl[T-t -1], betastarl[t])
            np.logaddexp.reduce(betastarl[t] + Al,axis=1,out=betal[t-1])
        betal[-1] = 0.

        return betal, betastarl

class HSMMStatesDynamicGeoApproximation(HSMMStatesGeoApproximation):
    pass # TODO

################################
#  use_eigen stuff below here  #
################################

# TODO TODO move away from weave and this ugliness

# default is python
HSMMStates = HSMMStatesPython
HMMStates = HMMStatesPython

def use_eigen(useit=True):
    global HSMMStates, HMMStates, hmm_sample_forwards_codestr, \
            hsmm_sample_forwards_codestr, hmm_messages_backwards_codestr, \
            eigen_path

    if useit:
        import os
        eigen_path = os.path.join(os.path.dirname(__file__),'../deps/Eigen3/')
        eigen_code_dir = os.path.join(os.path.dirname(__file__),'cpp_eigen_code/')
        with open(os.path.join(eigen_code_dir,'hsmm_sample_forwards.cpp')) as infile:
            hsmm_sample_forwards_codestr = infile.read()
        with open(os.path.join(eigen_code_dir,'hmm_messages_backwards.cpp')) as infile:
            hmm_messages_backwards_codestr = infile.read()
        with open(os.path.join(eigen_code_dir,'hmm_sample_forwards.cpp')) as infile:
            hmm_sample_forwards_codestr = infile.read()

            HSMMStates = HSMMStatesEigen
            HMMStates = HMMStatesEigen
    else:
        HSMMStates = HSMMStatesPython
        HMMStates = HMMStatesPython

