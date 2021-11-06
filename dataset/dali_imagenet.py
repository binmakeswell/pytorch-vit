from nvidia.dali.pipeline import Pipeline
from nvidia.dali.plugin.pytorch import DALIClassificationIterator, LastBatchPolicy
import nvidia.dali.fn as fn
import nvidia.dali.types as types
import nvidia.dali.tfrecord as tfrec
import os
import glob
import torch


class DaliDataloader(DALIClassificationIterator):
    def __init__(self,
                 tfrec_filenames,
                 tfrec_idx_filenames,
                 shard_id=0,
                 num_shards=1,
                 batch_size=128,
                 num_threads=4,
                 resize=256,
                 crop=224,
                 prefetch=2,
                 training=True,
                 gpu_aug=False,
                 cuda=True):
        pipe = Pipeline(batch_size=batch_size,
                        num_threads=num_threads,
                        device_id=torch.cuda.current_device() if cuda else None,
                        seed=1024)
        with pipe:
            inputs = fn.readers.tfrecord(
                path=tfrec_filenames,
                index_path=tfrec_idx_filenames,
                random_shuffle=training,
                shard_id=shard_id,
                num_shards=num_shards,
                initial_fill=10000,
                read_ahead=True,
                prefetch_queue_depth=prefetch,
                name='Reader',
                features={
                    'image/encoded': tfrec.FixedLenFeature((), tfrec.string, ""),
                    'image/class/label': tfrec.FixedLenFeature([1], tfrec.int64, -1),
                })
            jpegs = inputs["image/encoded"]

            if training:
                # decode jpeg and random crop
                images = fn.decoders.image_random_crop(jpegs,
                                                       device='mixed' if gpu_aug else 'cpu',
                                                       output_type=types.RGB,
                                                       random_aspect_ratio=[
                                                           crop / resize, resize / crop],
                                                       random_area=[
                                                           crop / resize, 1.0],
                                                       num_attempts=100
                                                       )
                images = fn.resize(images,
                                   device='gpu' if gpu_aug else 'cpu',
                                   resize_x=resize,
                                   resize_y=resize,
                                   dtype=types.FLOAT,
                                   interp_type=types.INTERP_TRIANGULAR)
                flip_lr = fn.random.coin_flip(probability=0.5)

                # additional training transforms
                images = fn.rotate(images, angle=fn.random.uniform(range=(-30, 30)),
                                   keep_size=True, fill_value=0)
                # ... https://docs.nvidia.com/deeplearning/dali/user-guide/docs/supported_ops.html
            else:
                # decode jpeg and resize
                images = fn.decoders.image(jpegs,
                                           device='mixed' if gpu_aug else 'cpu',
                                           output_type=types.RGB)
                images = fn.resize(images,
                                   device='gpu' if gpu_aug else 'cpu',
                                   resize_x=resize,
                                   resize_y=resize,
                                   dtype=types.FLOAT,
                                   interp_type=types.INTERP_TRIANGULAR)
                flip_lr = False

            # center crop and normalise
            images = fn.crop_mirror_normalize(images,
                                              dtype=types.FLOAT,
                                              crop=(crop, crop),
                                              mean=[0.485 * 255, 0.456 *
                                                    255, 0.406 * 255],
                                              std=[0.229 * 255, 0.224 *
                                                   255, 0.225 * 255],
                                              mirror=flip_lr)
            label = inputs["image/class/label"] - 1  # 0-999
            # LSG: element_extract will raise exception, let's flatten outside
            # label = fn.element_extract(label, element_map=0)  # Flatten
            if cuda:  # transfer data to gpu
                pipe.set_outputs(images.gpu(), label.gpu())
            else:
                pipe.set_outputs(images, label)

        pipe.build()
        last_batch_policy = 'DROP' if training else 'PARTIAL'
        super().__init__(pipe, reader_name="Reader",
                         auto_reset=True,
                         last_batch_policy=last_batch_policy)

    def __iter__(self):
        # if not reset (after an epoch), reset; if just initialize, ignore
        if self._counter >= self._size or self._size < 0:
            self.reset()
        return self

    def __next__(self):
        data = super().__next__()
        img, label = data[0]['data'], data[0]['label']
        label = label.squeeze()
        return img, label


class DaliImageNet(dict):
    def __init__(self, root,
                 shard_id=0, num_shards=1,
                 batch_size=128, num_threads=4,
                 prefetch=2,
                 gpu_aug=True):
        train_pat = os.path.join(root, 'train/*')
        train_idx_pat = os.path.join(root, 'idx_files/train/*')
        train = DaliDataloader(sorted(glob.glob(train_pat)),
                               sorted(glob.glob(train_idx_pat)),
                               shard_id=shard_id,
                               num_shards=num_shards,
                               batch_size=batch_size,
                               num_threads=num_threads,
                               prefetch=prefetch,
                               training=True,
                               cuda=True,
                               gpu_aug=gpu_aug)
        test_pat = os.path.join(root, 'validation/*')
        test_idx_pat = os.path.join(root, 'idx_files/validation/*')
        test = DaliDataloader(sorted(glob.glob(test_pat)),
                              sorted(glob.glob(test_idx_pat)),
                              shard_id=shard_id,
                              num_shards=num_shards,
                              batch_size=batch_size,
                              num_threads=num_threads,
                              prefetch=prefetch,
                              training=False,
                              cuda=True,
                              gpu_aug=gpu_aug)
        super().__init__(train=train, test=test)
