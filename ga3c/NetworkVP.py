# Copyright (c) 2016, NVIDIA CORPORATION. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import os
import re
import numpy as np
import tensorflow as tf
from tensorflow.contrib import rnn
from Config import Config


class NetworkVP:
    def __init__(self, device, model_name, num_actions):
        self.device = device
        self.model_name = model_name
        self.num_actions = num_actions

        self.img_width = Config.IMAGE_WIDTH
        self.img_height = Config.IMAGE_HEIGHT
        self.img_channels = Config.STACKED_FRAMES

        self.learning_rate = Config.LEARNING_RATE_START
        self.beta = Config.BETA_START
        self.log_epsilon = Config.LOG_EPSILON

        self.graph = tf.Graph()
        with self.graph.as_default() as g:
            with tf.device(self.device), tf.variable_scope('net_') as self.scope:
                self._create_graph()

                self.sess = tf.Session(
                    graph=self.graph,
                    config=tf.ConfigProto(
                        allow_soft_placement=True,
                        log_device_placement=False,
                        gpu_options=tf.GPUOptions(allow_growth=True)))
                self.sess.run(tf.global_variables_initializer())

                if Config.TENSORBOARD: self._create_tensor_board()
                if Config.LOAD_CHECKPOINT or Config.SAVE_MODELS:
                    vars = tf.global_variables()
                    self.saver = tf.train.Saver({var.name: var for var in vars}, max_to_keep=0)
                

    def _create_graph(self):
        self.x = tf.placeholder(tf.float32, [None, self.img_height, self.img_width, self.img_channels], name='X')
        self.y_r = tf.placeholder(tf.float32, [None], name='Yr')
        self.var_beta = tf.placeholder(tf.float32, name='beta', shape=[])
        self.var_learning_rate = tf.placeholder(tf.float32, name='lr', shape=[])
        self.global_step = tf.Variable(0, trainable=False, name='step')
        self.is_training = tf.placeholder(tf.bool)
        self.action_index = tf.placeholder(tf.float32, [None, self.num_actions])
        self.avg_score = tf.placeholder(tf.float32, name='avg_score')


        #self.d1 = self.jchoi_cnn(self.x)
        #self.d1 = tf.contrib.layers.flatten(self.x)
        self.d1 = tf.layers.dense(tf.contrib.layers.flatten(self.x), Config.NCELLS, activation=tf.nn.relu)

        #LSTM Layer
        if Config.USE_RNN:     
            D = Config.NCELLS
            self.lstm = rnn.LSTMCell(D, state_is_tuple=True) #or Basic
            self.step_sizes = tf.placeholder(tf.int32, [None], name='stepsize') 
            self.batch_size = tf.shape(self.step_sizes)[0]
            d1 = tf.reshape(self.d1, [self.batch_size,-1,D])

            self.c0 = tf.placeholder(tf.float32, [None, D])
            self.h0 = tf.placeholder(tf.float32, [None, D])
            self.initial_lstm_state = rnn.LSTMStateTuple(self.c0,self.h0)  
            lstm_outputs, self.lstm_state = tf.nn.dynamic_rnn(self.lstm,
                                                        d1,
                                                        initial_state = self.initial_lstm_state,
                                                        sequence_length = self.step_sizes,
                                                        time_major = False) 
                                                        #scope=scope)                                 
            self._state = tf.reshape(lstm_outputs, [-1,D]) #+ self.d1  #just in case, avoid vanishing gradient
            
        else:
            self._state = self.d1

        self.logits_v = tf.squeeze( tf.layers.dense(self._state, 1), axis=1)
        self.advantage_train = self.y_r - tf.stop_gradient(self.logits_v)

        if Config.CATEGORICAL:
            self.logits_p = tf.layers.dense(self.d1, self.num_actions)

            self.softmax_p = tf.nn.softmax(self.logits_p)
            self.log_softmax_p = tf.nn.log_softmax(self.logits_p)
            self.log_selected_action_prob = tf.reduce_sum(self.log_softmax_p * self.action_index, axis=1)
            self.sample_action = tf.squeeze(tf.multinomial(self.logits_p - tf.reduce_max(self.logits_p, 1, keep_dims=True), 1), squeeze_dims=1)

            self.policy_loss = self.log_selected_action_prob * self.advantage_train
            self.entropy = -1 * self.var_beta * tf.reduce_sum(self.log_softmax_p * self.softmax_p, axis=1)
        else:
            #self.mu = 2*tf.squeeze( tf.layers.dense(self._state,self.num_actions, activation=tf.nn.tanh, kernel_initializer=tf.zeros_initializer()), axis=1 )
            self.mu = tf.squeeze(tf.layers.dense(self._state, self.num_actions, kernel_initializer=tf.zeros_initializer()), axis=1)
            self.sigma = tf.squeeze( tf.layers.dense(self._state,1,activation=tf.nn.softplus, kernel_initializer=tf.ones_initializer()), axis=1 )


            action_taken = tf.squeeze(self.action_index, axis=1)
            self.sample_action = self.mu + tf.multiply(x=self.sigma, y=tf.random_normal(shape=tf.shape(self.mu)))
            #self.sample_action = tf.clip_by_value(self.sample_action, -2.0, 2.0)

            #derive log_prob: log(Normal(x))
            #derive entropy :  http://www.biopsychology.org/norwich/isp/chap8.pdf
            EPS = tf.constant(1e-5)
            self.l2_dist = tf.square(action_taken - self.mu)
            sqr_std_dev = tf.square(self.sigma)
            log_std_dev = tf.log(self.sigma + EPS)
            self.log_selected_action_prob = -self.l2_dist / (2 * sqr_std_dev + EPS) - 0.5 * tf.log(tf.constant(2 * np.pi)) - log_std_dev
            self.policy_loss = self.log_selected_action_prob * self.advantage_train
            self.entropy = self.var_beta * (log_std_dev + tf.constant(0.5 * np.log(2 * np.pi * np.e), tf.float32))


        mask = tf.reduce_max(self.action_index,axis=1)
        self.cost_v = 0.5 * tf.reduce_sum(tf.square(self.y_r - self.logits_v) * mask, axis=0)
        self.policy_loss_agg = tf.reduce_sum(self.policy_loss * mask, axis=0)
        self.entropy_agg = tf.reduce_sum(self.entropy * mask, axis=0)
        self.cost_p = -(self.policy_loss_agg + self.entropy_agg)


        self.cost_all = self.cost_p + self.cost_v

        print("Cost ALL SHAPE = ",self.cost_all.get_shape().as_list())
        self.opt = tf.train.AdamOptimizer(learning_rate=self.var_learning_rate)
        # self.opt = tf.train.RMSPropOptimizer(learning_rate=self.var_learning_rate,
        #                                     decay=Config.RMSPROP_DECAY,
        #                                     momentum=Config.RMSPROP_MOMENTUM,
        #                                     epsilon=Config.RMSPROP_EPSILON)

        self.opt_grad = self.opt.compute_gradients(self.cost_all)
        self.opt_grad_clipped = [(tf.clip_by_average_norm(g, Config.GRAD_CLIP_NORM),v) for g,v in self.opt_grad]
        self.train_op = self.opt.apply_gradients(self.opt_grad_clipped)


    def _create_tensor_board(self):
        summaries = tf.get_collection(tf.GraphKeys.SUMMARIES)
        summaries.append(tf.summary.scalar("Pcost_advantage", self.policy_loss_agg))
        summaries.append(tf.summary.scalar("Pcost_entropy", self.entropy_agg))
        summaries.append(tf.summary.scalar("Pcost", self.cost_p))
        summaries.append(tf.summary.scalar("Vcost", self.cost_v))
        summaries.append(tf.summary.scalar("LearningRate", self.var_learning_rate))
        summaries.append(tf.summary.scalar("Beta", self.var_beta))
        summaries.append(tf.summary.scalar("Reward_average", self.avg_score))  # somehow update score using ProcessStats?

        for var in tf.trainable_variables():
            summaries.append(tf.summary.histogram("weights_%s" % var.name, var))

        summaries.append(tf.summary.histogram("action taken", self.action_index))

        if Config.IMAGE_WIDTH > 1 and Config.IMAGE_HEIGHT > 1:
            vars = tf.trainable_variables()
            var = tf.transpose(vars[0], [3,0,1,2])
            varname = "weights_%s" % var.name
            summaries.append(tf.summary.image(varname, var,max_outputs=32))

        if Config.CATEGORICAL == False:
            summaries.append(tf.summary.histogram("mu", self.mu))
            summaries.append(tf.summary.histogram("sigma", self.sigma))
            summaries.append(tf.summary.histogram("l2dist", self.l2_dist))

        else:
            summaries.append(tf.summary.histogram("activation_p", self.logits_p))

        #for i,(g,v) in enumerate(self.opt_grad):
        #    summaries.append(tf.summary.histogram("gradnorm"+str(i), v))

        summaries.append(tf.summary.histogram("activation_d1", self.d1))
        summaries.append(tf.summary.histogram("activation_v", self.logits_v))

        self.summary_op = tf.summary.merge_all()
        self.log_writer = tf.summary.FileWriter("logs/%s" % self.model_name, self.sess.graph)

    def nips_cnn(self, _input):
        self.n1 = tf.contrib.layers.conv2d(self.n1, 16, 8, 4, activation_fn=tf.nn.elu)
        self.n2 = tf.contrib.layers.conv2d(self.n1, 32, 4, 2, activation_fn=tf.nn.elu)
        self.d1 = tf.contrib.layers.fully_connected(tf.contrib.layers.flatten(self.n2), Config.NCELLS,activation_fn=tf.nn.elu)
        return self.d1

    def jchoi_cnn(self, _input):
       self.n1 = tf.contrib.layers.conv2d(_input, 32, 3, 2, activation_fn=tf.nn.elu)
       self.n2 = tf.contrib.layers.conv2d(self.n1, 32, 3, 2, activation_fn=tf.nn.elu)
       self.n3 = tf.contrib.layers.conv2d(self.n2, 32, 3, 2, activation_fn=tf.nn.elu)
       self.n4 = tf.contrib.layers.conv2d(self.n3, 32, 3, 2, activation_fn=tf.nn.elu)
       self.d1 = tf.contrib.layers.fully_connected(tf.contrib.layers.flatten(self.n4), Config.NCELLS, activation_fn=tf.nn.elu)
       return self.d1

    def sep_cnn(self, _input):
        self.n1 = tf.contrib.layers.separable_conv2d(_input, 32, 3, depth_multiplier=2, stride=2, activation_fn=tf.nn.elu,normalizer_fn=tf.contrib.layers.layer_norm)
        self.n2 = tf.contrib.layers.separable_conv2d(self.n1, 64, 3, depth_multiplier=2, stride=2, activation_fn=tf.nn.elu,normalizer_fn=tf.contrib.layers.layer_norm)
        self.n3 = tf.contrib.layers.separable_conv2d(self.n2, 64, 3, depth_multiplier=1, stride=2, activation_fn=tf.nn.elu,normalizer_fn=tf.contrib.layers.layer_norm)
        self.n4 = tf.contrib.layers.separable_conv2d(self.n3, 64, 3, depth_multiplier=1, stride=2, activation_fn=tf.nn.elu,normalizer_fn=tf.contrib.layers.layer_norm)
        self.d1 = tf.contrib.layers.fully_connected(tf.contrib.layers.flatten(self.n4), Config.NCELLS,activation_fn=tf.nn.elu)
        return self.d1

    def __get_base_feed_dict(self):
        return {self.var_beta: self.beta, self.var_learning_rate: self.learning_rate, self.is_training: Config.TRAIN_MODELS}

    def get_global_step(self):
        step = self.sess.run(self.global_step)
        return step

    def predict_single(self, x):
        return self.predict_p(x[None, :])[0]

    def predict_v(self, x):
        prediction = self.sess.run(self.logits_v, feed_dict={self.x: x})
        return prediction

    def predict_p(self, x):
        prediction = self.sess.run(self.softmax_p, feed_dict={self.x: x})
        return prediction
    
    #rnn version
    def predict_a_and_v(self, x, c, h):
        feed_dict = self.__get_base_feed_dict()
        if Config.USE_RNN == False:     
            feed_dict.update({self.x: x, self.is_training: False})
            a, v = self.sess.run([self.sample_action, self.logits_v], feed_dict=feed_dict)
            return a, v, c, h
        else:
            step_sizes = np.ones((c.shape[0],),dtype=np.int32)       
            feed_dict = self.__get_base_feed_dict()
            feed_dict.update({self.x: x, self.step_sizes:step_sizes, self.c0:c, self.h0:h, self.is_training: False})
            a, v, rnn_state = self.sess.run([self.sample_action, self.logits_v, self.lstm_state], feed_dict=feed_dict)
            return a, v, rnn_state.c, rnn_state.h
    
    def train(self, x, y_r, a, c, h, l):
        # TODO : define a new OP which dynamically pad tensor
        # https://www.tensorflow.org/extend/adding_an_op
        r = np.reshape(y_r,(y_r.shape[0],))
        feed_dict = self.__get_base_feed_dict()

        #print("action taken in past = ", a)
        
        if Config.USE_RNN == False:        
            feed_dict.update({self.x: x, self.y_r: r, self.action_index: a, self.is_training: True})
        else:
            step_sizes = np.array(l)
            feed_dict.update({self.x: x, self.y_r: r, self.action_index: a, self.step_sizes:step_sizes, self.c0:c, self.h0:h, self.is_training: True})
        self.sess.run(self.train_op, feed_dict=feed_dict)

    def log(self, x, y_r, a, c, h, l, avg_score):
        r = np.reshape(y_r,(y_r.shape[0],))

        feed_dict = self.__get_base_feed_dict()
        if Config.USE_RNN == False:        
            feed_dict.update({self.x: x, self.y_r: r, self.action_index: a, self.is_training: True, self.avg_score:avg_score})
        else:
            step_sizes = np.array(l)
            feed_dict.update({self.x: x, self.y_r: r, self.action_index: a, self.step_sizes:step_sizes, self.c0:c, self.h0:h, self.batch_size:len(l),
                              self.is_training: True, self.avg_score:avg_score})

        step, summary = self.sess.run([self.global_step, self.summary_op], feed_dict=feed_dict)
        self.log_writer.add_summary(summary, step)

    def _checkpoint_filename(self, episode):
        return 'checkpoints/%s_%08d' % (self.model_name, episode)
    
    def _get_episode_from_filename(self, filename):
        # TODO: hacky way of getting the episode. ideally episode should be stored as a TF variable
        return int(re.split('/|_|\.', filename)[2])

    def save(self, episode):
        self.saver.save(self.sess, self._checkpoint_filename(episode))

    def load(self):
        filename = tf.train.latest_checkpoint(os.path.dirname(self._checkpoint_filename(episode=0)))
        if Config.LOAD_EPISODE > 0:
            filename = self._checkpoint_filename(Config.LOAD_EPISODE)
        self.saver.restore(self.sess, filename)
        print('loaded : ', filename)
        return self._get_episode_from_filename(filename)
       
    def get_variables_names(self):
        return [var.name for var in self.graph.get_collection('trainable_variables')]

    def get_variable_value(self, name):
        return self.sess.run(self.graph.get_tensor_by_name(name))
