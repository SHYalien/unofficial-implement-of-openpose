import os
import time
import logging
import argparse
import tensorflow as tf
from tensorflow.contrib import slim
from dataset import get_dataflow, batch_dataflow
from dataflow import COCODataPaths
import vgg
from cpm import CpmStage1
from pose_dataset import get_dataflow_batch, DataFlowToQueue, CocoPose
from pose_augment import set_network_input_wh, set_network_scale


def train(args, loss_func='org', use_bn=False):
    if args.not_continue_training:
        start_time = time.localtime(time.time())
        checkpoint_path = args.checkpoint_path + ('%d-%d-%d-%d-%d-%d' % start_time[0:6])
        os.mkdir(checkpoint_path)
    else:
        checkpoint_path = args.checkpoint_path

    logger = logging.getLogger('train')
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(checkpoint_path + '/train_log.log')
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.addHandler(fh)
    logger.info(args)
    logger.info('checkpoint_path: ' + checkpoint_path)

    # get training data
    # coco_data_val = COCODataPaths(annot_path=args.annot_path_val, img_dir=args.img_path_val)
    # coco_data_train = COCODataPaths(annot_path=args.annot_path_train, img_dir=args.img_path_train)
    # df = get_dataflow(coco_data_train)
    # batch_df = batch_dataflow(df, args.batch_size)

    # define input placeholder
    with tf.name_scope('inputs'):
        raw_img = tf.placeholder(tf.float32, shape=[args.batch_size, 368, 368, 3])
        # mask_hm = tf.placeholder(dtype=tf.float32, shape=[args.batch_size, 46, 46, args.hm_channels])
        # mask_cpm = tf.placeholder(dtype=tf.float32, shape=[args.batch_size, 46, 46, args.cpm_channels])
        hm = tf.placeholder(dtype=tf.float32, shape=[args.batch_size, 46, 46, args.hm_channels])
        cpm = tf.placeholder(dtype=tf.float32, shape=[args.batch_size, 46, 46, args.cpm_channels])

    # defien data loader
    logger.info('initializing data loader...')
    set_network_input_wh(args.input_width, args.input_height)
    scale = 8
    set_network_scale(scale)
    df = get_dataflow_batch(args.annot_path_train, True, args.batch_size, img_path=args.img_path_train)
    steps_per_echo = df.size()
    enqueuer = DataFlowToQueue(df, [raw_img, hm, cpm], queue_size=4)
    q_inp, q_heat, q_vect = enqueuer.dequeue()
    q_inp_split, q_heat_split, q_vect_split = tf.split(q_inp, 1), tf.split(q_heat, 1), tf.split(q_vect, 1)
    img_normalized = q_inp_split[0] / 255 - 0.5  # [-0.5, 0.5]

    logger.info('initializing model...')
    # define vgg19
    with slim.arg_scope(vgg.vgg_arg_scope()):
        vgg_outputs, end_points = vgg.vgg_19(img_normalized)

    # get net graph
    net = CpmStage1(inputs_x=vgg_outputs, stage_num=args.stage_num, hm_channel_num=args.hm_channels, use_bn=use_bn)
    hm_pre, cpm_pre, added_layers_out = net.gen_net()

    # 这个loss是其他版本代码里的实现
    losses = []
    with tf.name_scope('loss'):
        for idx, (l1, l2), in enumerate(zip(hm_pre, cpm_pre)):
            if loss_func == 'org':
                hm_loss = tf.reduce_sum(tf.square(tf.concat(l1, axis=0) - q_heat_split[0]))
                cpm_loss = tf.reduce_sum(tf.square(tf.concat(l2, axis=0) - q_vect_split[0]))
                losses.append(tf.reduce_sum([hm_loss, cpm_loss]))
            else:
                hm_loss = tf.nn.l2_loss(tf.concat(l1, axis=0) - q_heat_split[0])
                cpm_loss = tf.nn.l2_loss(tf.concat(l2, axis=0) - q_vect_split[0])
                losses.append(tf.reduce_mean([hm_loss, cpm_loss]))

        logger.info('use original loss in paper')
        loss = tf.reduce_sum(losses) / args.batch_size

    global_step = tf.Variable(0, name='global_step', trainable=False)
    learning_rate = tf.train.exponential_decay(1e-4, global_step, steps_per_echo, 0.5, staircase=True)
    trainable_var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='train_layers')
    with tf.name_scope('train'):
        train = tf.train.AdamOptimizer(learning_rate=learning_rate, epsilon=1e-8).minimize(loss=loss,
                                                                                           global_step=global_step,
                                                                                           var_list=trainable_var_list)
    logger.info('initialize saver...')
    restorer = tf.train.Saver(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='vgg_19'), name='vgg_restorer')
    saver = tf.train.Saver(trainable_var_list)

    logger.info('initialize tensorboard')
    tf.summary.scalar("lr", learning_rate)
    tf.summary.scalar("loss2", loss)
    tf.summary.histogram('hm_pre', hm_pre)
    tf.summary.histogram('img_normalized', img_normalized)
    tf.summary.histogram('vgg_outputs', vgg_outputs)
    tf.summary.histogram('added_layers_out', added_layers_out)
    tf.summary.image('vgg_out', tf.transpose(vgg_outputs[0:1, :, :, :], perm=[3, 1, 2, 0]), max_outputs=512)
    tf.summary.image('added_layers_out', tf.transpose(added_layers_out[0:1, :, :, :], perm=[3, 1, 2, 0]), max_outputs=128)
    tf.summary.image('cpm_gt', tf.transpose(q_vect_split[0][0:1, :, :, :], perm=[3, 1, 2, 0]), max_outputs=100)
    tf.summary.image('hm_gt', tf.transpose(q_heat_split[0][0:1, :, :, :], perm=[3, 1, 2, 0]), max_outputs=100)
    for i in range(args.stage_num - 1):
        tf.summary.image('hm_pre_stage_%d' % i, tf.transpose(hm_pre[i][0:1, :, :, :], perm=[3, 1, 2, 0]), max_outputs=19)
        tf.summary.image('cpm_pre_stage_%d' % i, tf.transpose(cpm_pre[i][0:1, :, :, :], perm=[3, 1, 2, 0]), max_outputs=38)
    tf.summary.image('input', img_normalized, max_outputs=4)
    # tf.summary.image('hm_mask', tf.transpose(mask_hm[0:1, :, :, :], perm=[3, 1, 2, 0]), max_outputs=19)

    logger.info('initialize session...')
    merged = tf.summary.merge_all()
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    with tf.Session(config=config) as sess:
        writer = tf.summary.FileWriter(checkpoint_path, sess.graph)
        sess.run(tf.group(tf.global_variables_initializer()))
        logger.info('restoring vgg weights...')
        restorer.restore(sess, args.backbone_net_ckpt_path)
        if not args.not_continue_training:
            saver.restore(sess, tf.train.latest_checkpoint(checkpoint_dir=checkpoint_path))
            logger.info('restoring from checkpoint...')
        logger.info('start training...')
        coord = tf.train.Coordinator()
        enqueuer.set_coordinator(coord)
        enqueuer.start()
        while True:
            total_loss, _, gs_num = sess.run([loss, train, global_step])
            echo = gs_num / steps_per_echo
            if gs_num % args.save_summary_frequency == 0:
                total_loss, gs_num, summary, lr = sess.run([loss, global_step, merged, learning_rate])
                writer.add_summary(summary, gs_num)
                logger.info('echos=%f, setp=%d, total_loss=%f, lr=%f' % (echo, gs_num, total_loss, lr))
            if gs_num % args.save_checkpoint_frequency == 0:
                saver.save(sess, save_path=checkpoint_path + '/' + 'model-%d.ckpt' % gs_num)
                logger.info('saving checkpoint to ' + checkpoint_path + '/' + 'model-%d.ckpt' % gs_num)
            if echo >= args.max_echos:
                break