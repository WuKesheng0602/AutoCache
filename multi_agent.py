#!/usr/bin/python3
from sys import argv
from os import path
import numpy as numpy
import tensorflow as tf
from queue import Queue
import multiprocessing as mp
from multiprocessing import Process, Pool

from utility import a3c, env
from utility.utility import load_trace

from params import *
NUM_AGENTS = mp.cpu_count()-1 # use n-1 core

global timestamps, bandwidths

def get_information(exp_q, actor_batch, critic_batch):
    s_batch, a_batch, r_batch, info = exp_q.get()
    actor_gradient, critic_gradient, td_batch = \
        a3c.compute_gradients(
            s_batch=np.stack(s_batch, axis=0),
            a_batch=np.vstack(a_batch),
            r_batch=np.vstack(r_batch),
            actor=actor, critic=critic
        )
    actor_batch.append(actor_gradient)
    critic_batch.append(critic_gradient)

    _len     = len(r_batch)
    _reward  = np.sum(r_batch)
    _td_loss = np.sum(td_batch)
    _entropy = np.sum(info['entropy'])
    return np.array([_len, _reward, _td_loss, _entropy])

def central_agent(params_qs, exp_qs, model):
    with tf.Session() as sess:
        actor   = a3c.ActorNetwork(sess,  A_DIM, [S_DIM, S_LEN], ACTOR_LRATE)
        critic  = a3c.CriticNetowkr(sess, A_DIM, [S_DIM, S_LEN], CRITIC_LRATE)

        summary_ops, summary_vars = a3c.BaseNetwork.build_summaries()
        sess.run(tf.global_variables_initializer())
        saver   = tf.train.Saver()  # save neural net parameters
        # writer  = tf.summary.FileWriter(SUMMARY_DIR, sess.graph)  # training monitor

        #NOTE: load intermidiate NN model
        epoch = 0
        if model:
            tmp = path.splittext(path.basename(model))
            epoch = int(tmp.split('_')[2])
            saver.restore(model)
            pass
        
        while True:
            # 1) synchronously distribute the network parameters
            actor_params  = actor.get_network_params()
            critic_params = critic.get_network_params()
            map(lambda q:q.put([actor_params, critic_params]), params_qs)

            # 2) update gradients
            actor_batch, critic_batch = list(), list()
            info_collection = \
                list(map(lambda q:get_information(q, actor_batch, critic_batch), params_qs))
            for a,c in zip(actor_batch, critic_batch):
                actor.apply_gradients(a)
                critic.apply_gradients(c)
                pass

            # 3) build summary
            total_batch_len, total_reward, total_td_loss, total_entropy = \ 
                tuple(np.sum(infor_collection, axis=0))
            avg_reward  = total_reward / float(len(info_collection))
            avg_td_loss = total_td_loss / total_batch_len
            avg_entropy = total_entropy / total_batch_len
            #TODO: write into logging file
            # summary_str = sess.run(summary_ops, feed_dict={
            #     summary_vars[0]: avg_td_loss,
            #     summary_vars[1]: avg_reward,
            #     summary_vars[2]: avg_entropy
            # })
            # writer.add_summary(summary_str, epoch)
            # writer.flush()

            # 4) next epoch and model saving
            epoch += 1
            if epoch % MODEL_SAVE_INTERVAL == 0: #save the NN model
                saver.save(sess, './model/nn_ep_%d.ckpt'%epoch)
                print('Model ep-%d Saved.'%epoch)
                pass
            pass
        pass
    pass

def agent(agent_id, params_q, exp_q):
    global timestamps, bandwidths
    net_env = env.Environment(timestamps, bandwidths, rnd_seed=agent_id)

    with tf.Session() as sess:
        actor   = a3c.ActorNetwork(sess,  A_DIM, [S_DIM, S_LEN], ACTOR_LRATE)
        critic  = a3c.CriticNetowkr(sess, A_DIM, [S_DIM, S_LEN], CRITIC_LRATE)

        acotr_params, critic_params = params_q.get() #block until get
        actor.set_network_params(acotr_params)
        critic.set_network_params(critic_params)

        #Init State, Action
        action_vec    = np.zeros(A_DIM)
        action_vec[0] = 1
        storage       = np.zeros(C_DIM)
        _state  = [np.zeros((S_INFO, S_LEN))]
        #Init Empty Batch Record (state,action,reward)
        s_batch = [np.zeros((S_INFO, S_LEN))]
        a_batch = [action_vec]
        r_batch, entropy_record = list(), list()

        _timer = 0
        while True:
            # 1) Env Simulation #FIXME: req_flag, req_file not used
            (req_flag, req_file, p1_delay, p2_delay,storage) = \
                net_env.whats_next(storage, action_vec)
            _reward = -p2_delay #NOTE: Maximize the MINUS cost
            r_batch.append(_reward)

            # 2) State Update
            # _state = np.array(s_batch[-1], copy=True) #FIXME: try uncomment if wrong 
            _state = np.roll(state, -1, axis=1)
            _state[0, -1] = p1_delay                #NOTE: last download time
            _state[1, -1] = SEG_SIZE/p1_delay       #NOTE: last download bandwidth
            _state[2, :C_DIM] = np.array(storage)   #NOTE: last storage

            # 3) Action Update
            action_prob = actor.predict(np,reshapre(_state, (1,S_DIM,S_LEN)))
            entropy_record.append(a3c.compute_entropy(action_prob[0])) #update entropy
            action_cumsum = np.cumsum(action_prob)
            action_idx = (action_cumsum > np.random.randint(1,1000)/1000.0).argmax()
            action_vec = np.zeros(A_DIM)
            action_vec[action_idx] = 1

            # 4) Report Experience
            if len(r_batch)>=TRAIN_SEQ_LEN:
                exp_q.put((
                    s_batch, a_batch, r_batch,
                    {'entropy': entropy_record}
                ))
                # SYNCHRONIZE the network parameters from coordinator
                actor_params, critic_params = params_q.get()
                actor.set_network_params(actor_params)
                critic.set_network_params(critic_params)
                # CLEAR the privious experience
                map(lambda x:x.clear(), [s_batch, a_batch, r_batch, entropy_record])
                pass
            
            # 5) Next Experience
            _timer += 1
            s_batch.append(_state)
            a_batch.append(action_vec)
            pass #end_of_while
        pass #end_of_session
    pass

def run(resume_model=None):
    global timestamps, bandwidths
    # Setup
    np.random.seed(42)
    params_qs = [Queue() for i in range(NUM_AGENTS)]
    exp_qs    = [Queue() for i in range(NUM_AGENTS)]
    timestamps, bandwidths = load_trace('./training_traces')

    # create a coordinator and multiple agent processes
    coordinator = Process(central_agent, args=(params_qs, exq_qs, resume_model))
    coordinator.start()
    # execute Pool of parallel agents (until the Poll is down)
    with Pool(NUM_AGENTS) as p:
        agent_params = zip(range(NUM_AGENTS), params_qs, exp_qs)
        p.map(agent, list(agent_params))
        pass
    pass

if __name__ == "__main__":
    try:
        if len(argv) > 1:
            run(argv[1])
        else:
            run()
    except Exception as e:
        print(e)
    finally:
        exit()
    pass