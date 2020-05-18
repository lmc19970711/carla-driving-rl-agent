from collections import OrderedDict

import os
import time
import carla
import pygame
import numpy as np

from typing import Optional, Tuple
from pygame.constants import K_q, K_UP, K_w, K_LEFT, K_a, K_RIGHT, K_d, K_DOWN, K_s, K_SPACE, K_ESCAPE, KMOD_CTRL

from tensorforce import Agent, util
from tensorforce.agents import PPOAgent, TensorforceAgent

from agents.specifications import Specifications as Specs
from agents.environment import SynchronousCARLAEnvironment, CARLAEvent, MyCARLAEnvironment

from navigation import LocalPlanner
from navigation.behavior_agent import BehaviorAgent


class Agents:
    """Provides predefined agents"""

    @staticmethod
    def get(kind: str, env: SynchronousCARLAEnvironment, *args, **kwargs):
        pass

    @staticmethod
    def pretraining(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps: int, speed: float, **kwargs):
        return PretrainingAgent(carla_env, max_episode_timesteps, speed, **kwargs)

    @staticmethod
    def behaviour_pretraining(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps: int,
                              ignore_traffic_light=False, traces_dir='data/traces', **kwargs):
        return PretrainingBehaviourAgent(carla_env, max_episode_timesteps, traces_dir, ignore_traffic_light, **kwargs)

    @staticmethod
    def validity_pretraining(carla_env: MyCARLAEnvironment, ignore_traffic_light=True, traces_dir='data/traces',
                             **kwargs):
        return ValidityPretrainAgent(carla_env, traces_dir, ignore_traffic_light, **kwargs)

    @staticmethod
    def validity_pretraining_no_skill(carla_env: MyCARLAEnvironment, ignore_traffic_light=True,
                                      traces_dir='data/traces', **kwargs):
        return ValidityPretrainAgentNoSkill(carla_env, traces_dir, ignore_traffic_light, **kwargs)

    @staticmethod
    def keyboard(carla_env: SynchronousCARLAEnvironment, fps=30.0, mode='play'):
        return KeyboardAgent(carla_env, fps, mode)

    @staticmethod
    def baseline(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps=512, batch_size=256, exploration=0.0,
                 update_frequency: Optional[int] = None, horizon: Optional[int] = None, discount=0.997, entropy=0.05,
                 name='baseline', **kwargs) -> Agent:
        horizon = horizon or (batch_size - 1)
        assert horizon < batch_size
        update_frequency = update_frequency or batch_size

        return Agent.create(agent='tensorforce',
                            name=name,
                            environment=carla_env,
                            max_episode_timesteps=max_episode_timesteps,

                            policy=Specs.policy(distributions='gaussian',
                                                network=Specs.network_v0(),
                                                temperature=0.99),

                            optimizer=dict(type='adam', learning_rate=3e-4),
                            objective=Specs.obj.policy_gradient(clipping_value=0.2),
                            update=Specs.update(unit='timesteps', batch_size=batch_size, frequency=update_frequency),

                            reward_estimation=dict(horizon=horizon,
                                                   discount=discount,
                                                   estimate_advantage=True),
                            exploration=exploration,
                            entropy_regularization=entropy,
                            **kwargs)

    @staticmethod
    def evolutionary(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps: int, batch_size=256, num_samples=6,
                     update_frequency: Optional[int] = None, decay_steps=768, filters=36, decay=0.995, lr=0.1,
                     units=(256, 128), layers=(2, 2), temperature=(0.9, 0.7), horizon: Optional[int] = None, width=140,
                     height=105, name='evolutionary', **kwargs) -> Agent:
        horizon = horizon or (batch_size - 1)
        assert horizon < batch_size

        policy_net = Specs.network_v1(conv=dict(stride=1, pooling='max', filters=filters),
                                      final=dict(layers=layers[0], units=units[0], activation='leaky-relu'))

        decay_lr = Specs.exp_decay(steps=decay_steps, unit='updates', initial_value=lr, rate=decay)

        critic_net = Specs.network_v1(conv=dict(stride=1, pooling='max', filters=filters),
                                      final=dict(layers=layers[1], units=units[1]))

        if 'preprocessing' in kwargs.keys():
            preprocessing = kwargs.pop('preprocessing')
        else:
            preprocessing = dict(image=[dict(type='image', width=width, height=height, grayscale=True),
                                        dict(type='exponential_normalization')])

        return Specs.carla_agent(carla_env, max_episode_timesteps,
                                 name=name,
                                 policy=dict(network=policy_net,
                                             optimizer=dict(type='evolutionary', num_samples=num_samples,
                                                            learning_rate=decay_lr),
                                             temperature=temperature[0]),

                                 batch_size=batch_size,
                                 update_frequency=update_frequency or batch_size,

                                 critic=dict(network=critic_net,
                                             optimizer=dict(type='adam', learning_rate=3e-3),
                                             temperature=temperature[1]),
                                 discount=1.0,
                                 horizon=horizon,

                                 preprocessing=preprocessing,

                                 summarizer=Specs.summarizer(frequency=update_frequency),

                                 entropy_regularization=Specs.exp_decay(steps=decay_steps, unit='updates',
                                                                        initial_value=lr, rate=decay),
                                 **kwargs)

    @staticmethod
    def ppo(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps: int, batch_size=1, subsampling_fraction=0.25,
            optimization_steps=10, discount=0.99, name='ppo', learning_rate=3e-4, critic_optimizer: dict = None,
            filters=32, dropout=0.0, final_units=200, **kwargs) -> PPOAgent:
        policy_net = Specs.network_v2(conv=dict(activation='leaky-relu', filters=filters),
                                      final=dict(activation='tanh', units=final_units),
                                      dropout=dropout)
        return Agent.create(agent='ppo',
                            name=name,
                            environment=carla_env,
                            max_episode_timesteps=max_episode_timesteps,
                            discount=discount,

                            network=policy_net,
                            learning_rate=learning_rate,
                            batch_size=batch_size,
                            subsampling_fraction=subsampling_fraction,
                            optimization_steps=optimization_steps,

                            critic_network=policy_net.copy(),
                            critic_optimizer=critic_optimizer or 'adam',
                            **kwargs)

    @staticmethod
    def ppo3(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps: int, time_horizon: int, batch_size=1,
             optimization_steps=10, discount=0.99, name='ppo3', learning_rate=3e-4, critic_optimizer: dict = None,
             dropout=0.0, subsampling_fraction=0.25, **kwargs) -> PPOAgent:
        assert time_horizon > 0
        t = time_horizon

        # TODO: get shapes from environment!
        policy_net = Specs.network_v3(features=dict(radar=dict(shape=(50, 40), filters=5, kernel=5, stride=2, layers=3),
                                                    road=dict(shape=(t, 10), filters=4, kernel=3, stride=1, layers=4),
                                                    vehicle=dict(shape=(t, 17), filters=4, kernel=(3, 4), layers=4),
                                                    past_actions=dict(shape=(t, 3), filters=5, kernel=(3, 1),
                                                                      layers=4)),
                                      final=dict(layers=2, units=256, activation='tanh'),  # 274 -> 256 -> 256 -> a_t
                                      dropout=dropout)

        return Agent.create(agent='ppo',
                            name=name,
                            environment=carla_env,
                            max_episode_timesteps=max_episode_timesteps,
                            discount=discount,

                            network=policy_net,
                            learning_rate=learning_rate,
                            batch_size=batch_size,
                            subsampling_fraction=subsampling_fraction,
                            optimization_steps=optimization_steps,

                            critic_network=policy_net.copy(),
                            critic_optimizer=critic_optimizer or 'adam',
                            **kwargs)

    @staticmethod
    def ppo4(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps: int, time_horizon: int, batch_size=1,
             optimization_steps=10, discount=0.99, name='ppo4', learning_rate=3e-4, critic_optimizer: dict = None,
             subsampling_fraction=0.25, entropy=0.05, clipping_steps=2000, **kwargs) -> PPOAgent:
        assert time_horizon > 0
        t = time_horizon

        # TODO: get shapes from the environment!
        # features = dict(road=dict(shape=(t, 10), filters=8, kernel=3, stride=1, layers=4),  # ~12k -> 32
        #                 vehicle=dict(shape=(t, 17), filters=8, kernel=(3, 4), layers=4),  # ~12k -> 28
        #                 past_actions=dict(shape=(t, 3), filters=8, kernel=(3, 1), layers=4))  # ~4k -> 32

        features = dict(road=dict(shape=(t, 10), filters=6, kernel=3, stride=1, layers=4),  # ~6k -> 24
                        vehicle=dict(shape=(t, 17), filters=6, kernel=(3, 4), layers=4),  # ~9k -> 24
                        past_actions=dict(shape=(t, 3), filters=6, kernel=(3, 1), layers=4))  # ~2k -> 24

        # ~333k -> 126, ~19k -> 40
        # conv_nets = dict(image=dict(filters=18, layers=(2, 5), middle_noise=True, middle_normalization=True),
        #                  radar=dict(filters=10, reshape=(50, 40, 1), layers=(2, 2), activation1='elu', noise=0.0))

        # ~495k -> 154, ~36k -> 56
        # conv_nets = dict(image=dict(filters=22, layers=(2, 5), middle_noise=True, middle_normalization=True),
        #                  radar=dict(filters=14, reshape=(50, 40, 1), layers=(2, 2), activation1='elu', noise=0.0))

        # ~410k -> 140, ~27k -> 48
        conv_nets = dict(image=dict(filters=22, layers=(2, 5), middle_noise=True, middle_normalization=True),
                         radar=dict(filters=12, reshape=(50, 40, 1), layers=(2, 2), activation1='elu', noise=0.0))

        # dense_nets = dict(past_skills=dict(units=32, activation='relu', layers=4))  # 32
        # dense_nets = dict(past_skills=dict(units=[24, 30, 30, 24], activation='relu'))  # 24
        dense_nets = dict(past_skills=dict(units=[24, 30, 30, 30, 24], activation='swish'))  # 24 -> ~3.6k

        # < 0.02ms (agent.act)
        policy_net = Specs.network_v4(convolutional=conv_nets, features=features, dense=dense_nets,
                                      final=dict(units=[320, 224, 224, 128], activation='swish'))  # 284 -> ~242k
                                      # final=dict(units=[320, 256, 224, 128], activation='swish'))  # >265k

        return Agent.create(agent='ppo',
                            name=name,
                            environment=carla_env,
                            max_episode_timesteps=max_episode_timesteps,
                            discount=discount,
                            likelihood_ratio_clipping=Specs.linear_decay(initial_value=0.25, final_value=0.0,
                                                                         steps=clipping_steps, cycle=True),
                            network=policy_net,
                            learning_rate=learning_rate,
                            batch_size=batch_size,
                            subsampling_fraction=subsampling_fraction,
                            optimization_steps=optimization_steps,

                            critic_network=policy_net.copy(),
                            critic_optimizer=critic_optimizer or 'adam',

                            entropy_regularization=Specs.exp_decay(unit='episodes', initial_value=entropy, rate=0.999,
                                                                   steps=1000),
                            variable_noise=0.0,
                            **kwargs)

    @staticmethod
    def ppo5(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps: int, radar_shape: Tuple[int, int],
             batch_size: int, optimization_steps=10, discount=0.99, name='ppo5', lr=1e-5, entropy=0.1, critic_lr=3e-5,
             subsampling_fraction=0.25, decay_steps=2000, **kwargs) -> PPOAgent:
        # get states and actions' shapes from the environment
        states_spec = carla_env.states()
        action_spec = carla_env.actions()

        critic_optimizer = dict(type='adam', learning_rate=critic_lr)

        features = dict(road=dict(shape=states_spec['road']['shape'], filters=6, kernel=3, stride=1, layers=4),
                        vehicle=dict(shape=states_spec['vehicle']['shape'], filters=6, kernel=(3, 4), layers=4),
                        past_actions=dict(shape=states_spec['past_actions']['shape'], filters=6, kernel=(3, 1),
                                          layers=4))

        conv_nets = dict(image=dict(filters=22, layers=(2, 5), middle_noise=True, middle_normalization=True),
                         radar=dict(filters=12, reshape=radar_shape + (1,), layers=(2, 2), activation1='elu', noise=0.0))

        dense_nets = dict(past_skills=dict(units=[24, 30, 30, 30, 24], activation='swish'))  # 24 -> ~3.6k

        # < 0.02ms (agent.act)
        policy_net = Specs.network_v4(convolutional=conv_nets, features=features, dense=dense_nets,
                                      final=dict(units=[320, 224, 224, 128], activation='swish'))  # 284 -> ~242k

        # the critic network takes as inputs the output embeddings of the policy network:
        # critic_net = Specs.network_v4(convolutional=dict(), features=dict(),
        #                               dense=dict(image_out=dict(units=[196, 120, 120, 70], activation='swish'),
        #                                          radar_out=dict(units=[64, 64, 24], activation='swish'),
        #                                          road_out=dict(units=[48, 48, 16], activation='tanh'),
        #                                          vehicle_out=dict(units=[48, 48, 16], activation='swish'),
        #                                          past_actions=dict(units=[48, 48, 16], activation='tanh'),
        #                                          past_skills=dict(units=[48, 48, 16], activation='tanh')),
        #                               final=dict(units=[212, 212, 128, 96, 48, 12], activation='swish'))
        critic_net = policy_net

        return Agent.create(agent='ppo', name=name,
                            states=states_spec,
                            actions=action_spec,
                            max_episode_timesteps=max_episode_timesteps,
                            discount=discount,
                            likelihood_ratio_clipping=Specs.linear_decay(initial_value=0.25, final_value=0.001,
                                                                         steps=decay_steps, cycle=True),
                            network=policy_net,
                            learning_rate=lr,
                            batch_size=batch_size,
                            subsampling_fraction=subsampling_fraction,
                            optimization_steps=optimization_steps,

                            critic_network=critic_net,
                            critic_optimizer=critic_optimizer,

                            entropy_regularization=Specs.linear_decay(initial_value=entropy, final_value=0.001,
                                                                      steps=decay_steps),
                            variable_noise=0.0,
                            **kwargs)

    @staticmethod
    def ppo6(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps: int, batch_size: int,
             optimization_steps=10, discount=0.99, name='ppo6', lr=1e-5, entropy=0.1, critic_lr=3e-5,
             subsampling_fraction=0.25, decay_steps=2000, use_same_optimizer=False, **kwargs) -> PPOAgent:
        """PP0-6"""
        critic_optimizer = dict(type='adam', learning_rate=critic_lr)

        if use_same_optimizer:
            critic_optimizer = dict(type='subsampling_step', optimizer=critic_optimizer, fraction=subsampling_fraction)
            critic_optimizer = dict(type='multi_step', optimizer=critic_optimizer, num_steps=optimization_steps)

        return Agent.create(agent='ppo', name=name,
                            environment=carla_env,
                            max_episode_timesteps=max_episode_timesteps,
                            discount=discount,
                            likelihood_ratio_clipping=Specs.linear_decay(initial_value=0.25, final_value=0.001,
                                                                         steps=decay_steps, cycle=True),
                            network=carla_env.policy_network(),
                            learning_rate=lr,
                            batch_size=batch_size,
                            subsampling_fraction=subsampling_fraction,
                            optimization_steps=optimization_steps,

                            critic_network=carla_env.policy_network(),
                            critic_optimizer=critic_optimizer,

                            entropy_regularization=Specs.linear_decay(initial_value=entropy, final_value=0.001,
                                                                      steps=decay_steps),
                            variable_noise=0.0,
                            **kwargs)

    @staticmethod
    def ppo7(carla_env: SynchronousCARLAEnvironment, batch_size: int, optimization_steps=10, discount=0.99, name='ppo7',
             lr=1e-5, entropy=0.1, critic_lr=3e-5, subsampling_fraction=0.25, decay_steps=2000, decay_lr=False,
             **kwargs) -> PPOAgent:
        """PP0-7"""
        if decay_lr:
            critic_lr = Specs.linear_decay(initial_value=critic_lr, final_value=critic_lr / 100, steps=decay_steps)
            lr = Specs.linear_decay(initial_value=lr, final_value=lr / 100, steps=decay_steps)

        critic_optimizer = dict(type='adam', learning_rate=critic_lr)
        critic_optimizer = dict(type='subsampling_step', optimizer=critic_optimizer, fraction=subsampling_fraction)
        critic_optimizer = dict(type='multi_step', optimizer=critic_optimizer, num_steps=optimization_steps)

        return Agent.create(agent='ppo', name=name,
                            environment=carla_env,
                            discount=discount,
                            likelihood_ratio_clipping=Specs.linear_decay(initial_value=0.25, final_value=0.001,
                                                                         steps=decay_steps, cycle=True),
                            network=carla_env.policy_network(),
                            learning_rate=lr,
                            batch_size=batch_size,
                            subsampling_fraction=subsampling_fraction,
                            optimization_steps=optimization_steps,

                            critic_network=carla_env.policy_network(),
                            critic_optimizer=critic_optimizer,

                            entropy_regularization=Specs.linear_decay(initial_value=entropy, final_value=0.001,
                                                                      steps=decay_steps),
                            variable_noise=0.0,
                            **kwargs)

    @staticmethod
    def ppo8(carla_env: MyCARLAEnvironment, batch_size: int, optimization_steps=10, discount=0.99, name='ppo8',
             lr=1e-5, entropy=0.1, critic_lr=3e-5, subsampling_fraction=0.25, decay: dict = None, clipping=0.2,
             noise=0.0, optimizer='adam', **kwargs) -> PPOAgent:
        """PP0-8"""
        decay = decay if isinstance(decay, dict) else dict()
        decay_lr = decay.get('lr')
        decay_clipping = decay.get('clipping')
        decay_entropy = decay.get('entropy')
        decay_noise = decay.get('noise')

        # Learning rate decay
        if isinstance(decay_lr, dict):
            if decay_lr.pop('type', None) == 'exp':
                lr = Specs.exp_decay(initial_value=lr, unit='updates', **decay_lr)

                critic_lr = Specs.exp_decay(initial_value=critic_lr, steps=decay_lr['steps'], unit='updates',
                                            **decay_lr)
            else:
                lr = Specs.linear_decay(initial_value=lr, final_value=lr / 100, unit='updates', **decay_lr)

                critic_lr = Specs.linear_decay(initial_value=critic_lr, final_value=critic_lr / 100, unit='updates',
                                               **decay_lr)

        critic_optimizer = dict(type=optimizer, learning_rate=critic_lr)
        critic_optimizer = dict(type='subsampling_step', optimizer=critic_optimizer, fraction=subsampling_fraction)
        critic_optimizer = dict(type='multi_step', optimizer=critic_optimizer, num_steps=optimization_steps)

        # Likelihood ratio clipping decay:
        if isinstance(decay_clipping, dict):
            if decay_clipping.pop('type', None) == 'exp':
                clipping = Specs.exp_decay(initial_value=clipping, unit='updates', **decay_clipping)
            else:
                clipping = Specs.linear_decay(initial_value=clipping, final_value=0.0,
                                              unit=decay_clipping.get('unit', 'updates'), **decay_clipping)

        # Entropy decay:
        if isinstance(decay_entropy, dict):
            if decay_entropy.pop('type', None) == 'exp':
                entropy = Specs.exp_decay(initial_value=entropy, unit='updates', **decay_entropy)
            else:
                entropy = Specs.linear_decay(initial_value=entropy, unit='updates', **decay_entropy)

        # Gaussian noise decay:
        if isinstance(decay_noise, dict):
            if decay_noise.pop('type', None) == 'exp':
                noise = Specs.exp_decay(initial_value=noise, unit='updates', **decay_noise)
            else:
                noise = Specs.linear_decay(initial_value=noise, final_value=0.0, unit='updates', **decay_noise)

        return Agent.create(agent='ppo', name=name,
                            environment=carla_env,
                            discount=discount,
                            likelihood_ratio_clipping=clipping,
                            network=carla_env.policy_network(),
                            learning_rate=lr,
                            batch_size=batch_size,
                            subsampling_fraction=subsampling_fraction,
                            optimization_steps=optimization_steps,

                            critic_network=carla_env.policy_network(),
                            critic_optimizer=critic_optimizer,

                            entropy_regularization=entropy,
                            exploration=noise,
                            variable_noise=0.0,
                            **kwargs)

    @staticmethod
    def ppo9(carla_env: MyCARLAEnvironment, horizon: int, batch_size: int, optimization_steps=10, discount=0.99,
             name='ppo9', lr=1e-5, entropy=0.1, critic_lr=3e-5, subsampling_fraction=0.25, decay: dict = None,
             clipping=0.2, noise=0.0, huber_loss=0.0, optimizer='adam', **kwargs) -> TensorforceAgent:
        """PP0-9"""
        assert batch_size > 0
        assert horizon < batch_size

        decay = decay if isinstance(decay, dict) else dict()
        decay_lr = decay.get('lr')
        decay_clipping = decay.get('clipping')
        decay_entropy = decay.get('entropy')
        decay_noise = decay.get('noise')

        # Learning rate decay
        if isinstance(decay_lr, dict):
            if decay_lr.pop('type', None) == 'exp':
                lr = Specs.exp_decay(initial_value=lr, unit='updates', **decay_lr)

                critic_lr = Specs.exp_decay(initial_value=critic_lr, steps=decay_lr['steps'], unit='updates',
                                            **decay_lr)
            else:
                lr = Specs.linear_decay(initial_value=lr, final_value=lr / 100, unit='updates', **decay_lr)

                critic_lr = Specs.linear_decay(initial_value=critic_lr, final_value=critic_lr / 100, unit='updates',
                                               **decay_lr)

        policy_optimizer = dict(type=optimizer, learning_rate=lr)
        policy_optimizer = dict(type='subsampling_step', optimizer=policy_optimizer, fraction=subsampling_fraction)
        policy_optimizer = dict(type='multi_step', optimizer=policy_optimizer, num_steps=optimization_steps)

        critic_optimizer = dict(type=optimizer, learning_rate=critic_lr)
        critic_optimizer = dict(type='subsampling_step', optimizer=critic_optimizer, fraction=subsampling_fraction)
        critic_optimizer = dict(type='multi_step', optimizer=critic_optimizer, num_steps=optimization_steps)

        # Likelihood ratio clipping decay:
        if isinstance(decay_clipping, dict):
            if decay_clipping.pop('type', None) == 'exp':
                clipping = Specs.exp_decay(initial_value=clipping, unit='updates', **decay_clipping)
            else:
                clipping = Specs.linear_decay(initial_value=clipping, final_value=0.0,
                                              unit=decay_clipping.get('unit', 'updates'), **decay_clipping)

        # Entropy decay:
        if isinstance(decay_entropy, dict):
            if decay_entropy.pop('type', None) == 'exp':
                entropy = Specs.exp_decay(initial_value=entropy, unit='updates', **decay_entropy)
            else:
                entropy = Specs.linear_decay(initial_value=entropy, unit='updates', **decay_entropy)

        # Gaussian noise decay:
        if isinstance(decay_noise, dict):
            if decay_noise.pop('type', None) == 'exp':
                noise = Specs.exp_decay(initial_value=noise, unit='updates', **decay_noise)
            else:
                noise = Specs.linear_decay(initial_value=noise, final_value=0.0, unit='updates', **decay_noise)

        Agent.create(agent='tensorforce',
                     name=name,
                     environment=carla_env,
                     update=dict(unit='timesteps', batch_size=batch_size),

                     # Policy
                     policy=dict(network=carla_env.policy_network(),
                                 # distributions='gaussian',
                                 use_beta_distribution=True,
                                 temperature=0.99,
                                 infer_states_value=False),
                     memory=dict(type='recent'),
                     optimizer=policy_optimizer,
                     objective=Specs.obj.policy_gradient(clipping_value=clipping, ratio_based=True),

                     # Critic
                     baseline_policy=dict(network=carla_env.policy_network(),
                                          distributions=dict(float='gaussian'),
                                          temperature=0.0,
                                          use_beta_distribution=False,
                                          infer_state_value='action-values'),
                     baseline_optimizer=critic_optimizer,
                     baseline_objective=Specs.obj.value(value='action', huber_loss=huber_loss),

                     # Reward
                     reward_estimation=dict(discount=discount,
                                            horizon=horizon,
                                            estimate_horizon='early',
                                            estimate_terminal=True,
                                            estimate_advantage=True),
                     # Exploration
                     entropy_regularization=entropy,
                     exploration=noise,
                     variable_noise=0.0,
                     **kwargs)

    @staticmethod
    def ppo_like(carla_env: SynchronousCARLAEnvironment, max_episode_timesteps: int, policy: dict, name='ppo_like',
                 critic: Optional[dict] = None, batch_size=64, update_frequency: Optional[int] = None, huber_loss=0.0,
                 discount=1.0, horizon: Optional[int] = None, estimate_terminal=False, **kwargs) -> Agent:
        horizon = horizon or (batch_size - 1)
        assert horizon < batch_size
        update_frequency = update_frequency or batch_size

        if critic is None:
            critic_policy = critic_optimizer = critic_objective = None
        else:
            critic_policy = dict(network=critic.get('network', None))
            critic_optimizer = critic.get('optimizer', dict(type='adam', learning_rate=3e-3))
            critic_objective = Specs.obj.value(value='state', huber_loss=huber_loss)

        return Agent.create(agent='tensorforce',
                            name=name,
                            environment=carla_env,
                            max_episode_timesteps=max_episode_timesteps,
                            update=Specs.update(unit='timesteps', batch_size=batch_size, frequency=update_frequency),

                            # Policy
                            policy=Specs.policy(network=policy.get('network'),
                                                distributions=policy.get('distributions', None),
                                                temperature=policy.get('temperature', 1.0),
                                                infer_states_value=policy.get('infer_states', False)),
                            memory=dict(type='recent'),
                            optimizer=policy.get('optimizer', dict(type='adam', learning_rate=3e-4)),
                            objective=Specs.obj.policy_gradient(clipping_value=0.2, ratio_based=True),

                            # Critic
                            baseline_policy=critic_policy,
                            baseline_optimizer=critic_optimizer,
                            baseline_objective=critic_objective,

                            # Reward
                            reward_estimation=dict(discount=discount,
                                                   horizon=horizon,
                                                   estimate_horizon=False if critic is None else 'early',
                                                   estimate_terminal=estimate_terminal,
                                                   estimate_advantage=True),
                            **kwargs)


# -------------------------------------------------------------------------------------------------
# -- Dummy Agents
# -------------------------------------------------------------------------------------------------

class DummyAgent(object):

    def reset(self):
        pass

    def act(self, states, **kwargs):
        raise NotImplementedError

    def observe(self, reward, terminal=False, **kwargs):
        raise NotImplementedError

    def load(self, **kwargs):
        raise NotImplementedError('Unsupported operation!')

    def save(self, **kwargs):
        raise NotImplementedError('Unsupported operation!')


class PretrainingAgent(DummyAgent):
    """A dummy agent whose only purpose is to record traces for pretraining other agents"""

    def __init__(self, env: SynchronousCARLAEnvironment, max_episode_timesteps: int, speed=30.0, use_speed_limit=False,
                 traces_dir=None, **kwargs):
        self.agent = Agent.create(agent='constant',
                                  name='pretraining',
                                  environment=env,
                                  max_episode_timesteps=max_episode_timesteps,
                                  action_values=env.DEFAULT_ACTIONS,
                                  recorder=dict(directory=traces_dir) if isinstance(traces_dir, str) else None,
                                  **kwargs)
        self.env = env
        self.index = 0
        self.max_timesteps = max_episode_timesteps
        self.use_speed_limit = use_speed_limit

        # Behaviour planner:
        self.options = dict(target_speed=speed,
                            lateral_control_dict={'K_P': 1, 'K_D': 0.02, 'K_I': 0, 'dt': 1.0 / self.env.fps})

        self.local_planner = None

        # register to environment's events:
        self.env.register_event(event=CARLAEvent.RESET, callback=self.reset)

    def reset(self):
        print('agent.reset')
        self.index = 0
        self.local_planner = LocalPlanner(vehicle=self.env.vehicle, opt_dict=self.options)

    def act(self, states, **kwargs):
        if self.use_speed_limit:
            self.local_planner.set_speed(speed=self.env.vehicle.get_speed_limit())

        # hack: records states
        _ = self.agent.act(states, **kwargs)

        control = self._run_step()
        actions, skill_name = self.env.control_to_actions(control)

        # hack: record "custom" (not constant) actions
        if isinstance(actions, dict):
            for name in self.agent.actions_spec.keys():
                self.agent.actions_buffers[name][0, self.index] = actions[name]
        else:
            for name in self.agent.actions_spec.keys():
                self.agent.actions_buffers[name][0, self.index] = actions

        self.index = (self.index + 1) % self.max_timesteps
        return actions

    def load(self, **kwargs):
        super().load(**kwargs)

    def save(self, **kwargs):
        super().save(**kwargs)

    def observe(self, reward, terminal=False, **kwargs):
        # hack: record rewards and terminals
        return self.agent.observe(reward, terminal=terminal, **kwargs)

    def _run_step(self) -> carla.VehicleControl:
        """Execute one step of navigation. WARNING: does not check for obstacles and traffic lights!
            :return: carla.VehicleControl
        """
        return self.local_planner.run_step(debug=False)


class PretrainingBehaviourAgent(DummyAgent):
    """A dummy agent whose only purpose is to record traces for pretraining other agents."""

    def __init__(self, env: SynchronousCARLAEnvironment, max_episode_timesteps: int, traces_dir=None,
                 ignore_traffic_light=False, behavior='cautious', **kwargs):
        self.agent = Agent.create(agent='constant', name='pretraining-behaviour',
                                  environment=env,
                                  max_episode_timesteps=max_episode_timesteps,
                                  action_values=env.DEFAULT_ACTIONS,
                                  recorder=dict(directory=traces_dir) if isinstance(traces_dir, str) else None,
                                  **kwargs)
        self.env = env
        self.index = 0
        self.max_timesteps = max_episode_timesteps

        # Behaviour planner:
        self.bh_agent = None
        self.args = dict(ignore_traffic_light=ignore_traffic_light, behavior=behavior,
                         min_route_size=max_episode_timesteps)

        # register to environment's events:
        self.env.register_event(event=CARLAEvent.RESET, callback=self.reset)

    def reset(self):
        print('agent.reset')
        self.index = 0

        # if self.bh_agent is None:
        #     self.bh_agent = BehaviorAgent(vehicle=self.env.vehicle, **self.args)

        self.bh_agent = BehaviorAgent(vehicle=self.env.vehicle, **self.args)
        self.bh_agent.set_destination(start_location=self.env.spawn_point.location,
                                      end_location=self.env.destination, clean=True)

    def act(self, states, **kwargs):
        self.bh_agent.update_information()

        # hack: records states
        _ = self.agent.act(states, **kwargs)

        control = self._run_step()
        actions, skill_name = self.env.control_to_actions(control)

        # hack: record "custom" (not constant) actions
        if isinstance(actions, dict):
            for name in self.agent.actions_spec.keys():
                self.agent.actions_buffers[name][0, self.index] = actions[name]
        else:
            for name in self.agent.actions_spec.keys():
                self.agent.actions_buffers[name][0, self.index] = actions

        self.index = (self.index + 1) % self.max_timesteps
        return actions

    def load(self, **kwargs):
        super().load(**kwargs)

    def save(self, **kwargs):
        super().save(**kwargs)

    def observe(self, reward, terminal=False, **kwargs):
        # hack: record rewards and terminals
        return self.agent.observe(reward, terminal=terminal, **kwargs)

    def _run_step(self) -> carla.VehicleControl:
        """Execute one step of navigation. WARNING: does not check for obstacles and traffic lights!
            :return: carla.VehicleControl
        """
        return self.bh_agent.run_step(debug=False)


class ValidityPretrainAgent:
    def __init__(self, env: MyCARLAEnvironment, traces_dir=None, ignore_traffic_light=False, behavior='cautious',
                 reward_threshold=15.0, **kwargs):
        self.env = env
        self.max_timesteps = env.max_episode_timesteps()
        self.index = 0
        self.traces_dir = traces_dir
        self.episodes = 0
        self.reward_threshold = reward_threshold

        self.states_spec = util.valid_values_spec(
            values_spec=self.env.states(), value_type='state', return_normalized=True)

        self.actions_spec = util.valid_values_spec(
            values_spec=self.env.actions(), value_type='action', return_normalized=True)

        # buffers:
        self.terminal_buffers = None
        self.reward_buffers = None
        self.states_buffers = None
        self.actions_buffers = None

        self.record_states = None
        self.record_actions = None
        self.record_terminal = None
        self.record_reward = None

        # Behaviour planner:
        self.agent = None
        self.args = dict(ignore_traffic_light=ignore_traffic_light, behavior=behavior,
                         min_route_size=self.max_timesteps)

        # register to environment's events:
        self.env.register_event(event=CARLAEvent.RESET, callback=self.reset)

    def init_buffers(self):
        self.terminal_buffers = np.ndarray(shape=(1, self.max_timesteps), dtype=util.np_dtype(dtype='long'))
        self.reward_buffers = np.ndarray(shape=(1, self.max_timesteps), dtype=util.np_dtype(dtype='float'))
        self.states_buffers = OrderedDict()
        self.actions_buffers = OrderedDict()

        for name, spec in self.states_spec.items():
            shape = (1, self.max_timesteps) + spec['shape']
            self.states_buffers[name] = np.ndarray(shape=shape, dtype=util.np_dtype(dtype=spec['type']))

        for name, spec in self.actions_spec.items():
            shape = (1, self.max_timesteps) + spec['shape']
            self.actions_buffers[name] = np.ndarray(shape=shape, dtype=util.np_dtype(dtype=spec['type']))

    def init_records(self):
        self.record_states = OrderedDict(((name, list()) for name in self.states_spec))
        self.record_actions = OrderedDict(((name, list()) for name in self.actions_spec))
        self.record_terminal = list()
        self.record_reward = list()

    def reset(self):
        print('agent.reset')
        self.init_buffers()
        self.init_records()
        self.index = 0

        self.agent = BehaviorAgent(vehicle=self.env.vehicle, **self.args)
        self.agent.set_destination(start_location=self.env.vehicle.get_location(), end_location=self.env.destination,
                                   clean=True)

    def act(self, states):
        assert isinstance(states, dict)
        self.agent.update_information()

        control = self._run_step()
        actions, skill_name = self.env.control_to_actions(control)

        # record states and actions:
        for name in self.states_spec.keys():
            self.states_buffers[name][0, self.index] = states[name]

        for name in self.actions_spec.keys():
            self.actions_buffers[name][0, self.index] = actions[name]

        # self.index = (self.index + 1) % self.max_timesteps
        return actions

    def write_trace(self):
        index = self.max_timesteps

        if sum(self.record_reward) < self.reward_threshold * len(self.record_reward):
            filename = 'trace-{}-{}.npz'.format(self.episodes, time.strftime('%Y%m%d-%H%M%S'))
            print(f'{filename} not saved because reward {round(sum(self.record_reward), 2)} < {self.reward_threshold * len(self.record_reward)}')
            return

        # init records
        for name in self.states_spec:
            self.record_states[name].append(np.array(self.states_buffers[name][0, :index]))

        for name, spec in self.actions_spec.items():
            self.record_actions[name].append(np.array(self.actions_buffers[name][0, :index]))

        self.record_terminal.append(np.array(self.terminal_buffers[0, :index]))
        self.record_reward.append(np.array(self.reward_buffers[0, :index]))

        # init directory
        if os.path.isdir(self.traces_dir):
            files = sorted(
                f for f in os.listdir(self.traces_dir)
                if os.path.isfile(os.path.join(self.traces_dir, f))
                and f.startswith('trace-'))
        else:
            os.makedirs(self.traces_dir)
            files = list()

        filename = 'trace-{}-{}.npz'.format(self.episodes, time.strftime('%Y%m%d-%H%M%S'))
        filename = os.path.join(self.traces_dir, filename)

        self.record_states = util.fmap(
            function=np.concatenate, xs=self.record_states, depth=1)

        self.record_actions = util.fmap(
            function=np.concatenate, xs=self.record_actions, depth=1)

        self.record_terminal = np.concatenate(self.record_terminal)
        self.record_reward = np.concatenate(self.record_reward)

        np.savez_compressed(
            filename, **self.record_states, **self.record_actions,
            terminal=self.record_terminal, reward=self.record_reward)

    def observe(self, reward, terminal=False):
        # hack: record rewards and terminals
        self.reward_buffers[0, self.index] = reward
        self.terminal_buffers[0, self.index] = terminal
        self.index += 1

        if terminal:
            self.episodes += 1
            self._compute_action_validity()
            self.write_trace()

    def _compute_action_validity(self):
        # set right value for validity; adjust rewards accordingly:
        control_buffer = self.actions_buffers['control']

        for i in range(len(control_buffer)):
            validity = 1
            rewards = []

            for j in range(i, len(control_buffer) - 1):
                if control_buffer[0, j] == control_buffer[0, j + 1]:
                    validity += 1
                    rewards.append(self.reward_buffers[0, j])
                else:
                    self.reward_buffers[0, i:j] = self.env.aggregate_reward(rewards, r_max=150.0, r_min=-2000)
                    break

            self.actions_buffers['validity'][0, i] = validity

    def _run_step(self) -> carla.VehicleControl:
        """Execute one step of navigation. WARNING: does not check for obstacles and traffic lights!
            :return: carla.VehicleControl
        """
        return self.agent.run_step(debug=False)


class ValidityPretrainAgentNoSkill(ValidityPretrainAgent):

    def act(self, states):
        assert isinstance(states, dict)
        self.agent.update_information()

        control = self._run_step()
        actions = self.env.control_to_actions(control)

        # record states and actions:
        for name in self.states_spec.keys():
            self.states_buffers[name][0, self.index] = states[name]

        for name in self.actions_spec.keys():
            self.actions_buffers[name][0, self.index] = actions[name]

        return actions

    def observe(self, reward, terminal=False):
        # hack: record rewards and terminals
        self.reward_buffers[0, self.index] = reward
        self.terminal_buffers[0, self.index] = terminal
        self.index += 1

        if terminal:
            self.episodes += 1
            # self._compute_action_validity()
            self.write_trace()


class KeyboardAgent(DummyAgent):
    """Two modes: 'play', and 'pretrain/record'
        - in "play mode" a human controls the given agent with a keyboard,
        - in "pretrain mode" a human controls the agent with a keyboard and records its actions plus the states.
    """

    # TODO: implement 'record' mode
    def __init__(self, env: SynchronousCARLAEnvironment, fps=30.0, mode='play'):
        self.env = env
        self.mode = mode
        self.fps = fps

        self.control = carla.VehicleControl()
        self._steer_cache = 0.0

    def reset(self):
        self.control = carla.VehicleControl()
        self._steer_cache = 0.0

    def act(self, states, **kwargs):
        return self._parse_events()

    def observe(self, reward, terminal=False, **kwargs):
        return False

    def _parse_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.close()

            elif event.type == pygame.KEYUP:
                if self._is_quit_shortcut(event.key):
                    self.close()

                elif event.key == K_q:
                    self.control.gear = 1 if self.control.reverse else -1

        return self._parse_vehicle_keys()

    @staticmethod
    def _is_quit_shortcut(key):
        return (key == K_ESCAPE) or (key == K_q and pygame.key.get_mods() & KMOD_CTRL)

    def _parse_vehicle_keys(self):
        keys = pygame.key.get_pressed()
        steer_increment = 5e-4 * self.env.clock.get_time()

        if keys[K_LEFT] or keys[K_a]:
            if self._steer_cache > 0:
                self._steer_cache = 0
            else:
                self._steer_cache -= steer_increment

        elif keys[K_RIGHT] or keys[K_d]:
            if self._steer_cache < 0:
                self._steer_cache = 0
            else:
                self._steer_cache += steer_increment
        else:
            self._steer_cache = 0.0

        self._steer_cache = min(1.0, max(-1.0, self._steer_cache))
        self.control.reverse = self.control.gear < 0

        # actions
        throttle = 1.0 if keys[K_UP] or keys[K_w] else 0.0
        steer = round(self._steer_cache, 1)
        brake = 1.0 if keys[K_DOWN] or keys[K_s] else 0.0
        reverse = 1.0 if self.control.reverse else 0.0
        hand_brake = keys[K_SPACE]

        return [throttle, steer, brake, reverse, hand_brake]

    def close(self):
        raise Exception('closing...')

    def load(self, **kwargs):
        super().load(**kwargs)

    def save(self, **kwargs):
        super().save(**kwargs)
