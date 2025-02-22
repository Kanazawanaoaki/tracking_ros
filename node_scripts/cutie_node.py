#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np
import torch
import torch.nn.functional as F
import supervision as sv

import rospy
from cv_bridge import CvBridge
from sensor_msgs.msg import Image

from model_config import CutieConfig
from utils import overlay_davis


class CutieNode(object):  # should not be ConnectionBasedNode cause Cutie tracker needs continuous input
    def __init__(self):
        super(CutieNode, self).__init__()
        self.cutie_config = CutieConfig.from_rosparam()
        self.predictor = self.cutie_config.get_predictor()
        self.with_bbox = rospy.get_param("~with_bbox", False)

        self.bridge = CvBridge()
        self.initialize()

        self.sub_image = rospy.Subscriber(
            "~input_image",
            Image,
            self.callback,
            queue_size=1,
            buff_size=2**24,
        )
        self.pub_vis_img = rospy.Publisher("~output/segmentation_image", Image, queue_size=1)
        self.pub_segmentation_img = rospy.Publisher("~output/segmentation", Image, queue_size=1)

    @torch.inference_mode()
    def initialize(self):
        # oneshot subscribe initial image and segmentation
        input_seg_msg = rospy.wait_for_message("~input_segmentation", Image)
        self.mask = self.bridge.imgmsg_to_cv2(input_seg_msg, desired_encoding="32SC1")
        input_img_msg = rospy.wait_for_message("~input_image", Image)
        self.image = self.bridge.imgmsg_to_cv2(input_img_msg, desired_encoding="rgb8")

        # initialize the model with the mask
        with torch.cuda.amp.autocast(enabled=True):
            image_torch = (
                torch.from_numpy(self.image.transpose(2, 0, 1)).float().to(self.cutie_config.device, non_blocking=True)
                / 255
            )
            # initialize with the mask
            mask_torch = (
                F.one_hot(
                    torch.from_numpy(self.mask).long(),
                    num_classes=len(np.unique(self.mask)),
                )
                .permute(2, 0, 1)
                .float()
                .to(self.cutie_config.device)
            )
            # the background mask is not fed into the model
            self.mask = self.predictor.step(image_torch, mask_torch[1:], idx_mask=False)

    def publish_result(self, mask, vis, frame_id):
        if mask is not None:
            seg_msg = self.bridge.cv2_to_imgmsg(mask.astype(np.int32), encoding="32SC1")
            seg_msg.header.stamp = rospy.Time.now()
            seg_msg.header.frame_id = frame_id
            self.pub_segmentation_img.publish(seg_msg)
        if vis is not None:
            vis_img_msg = self.bridge.cv2_to_imgmsg(vis, encoding="rgb8")
            vis_img_msg.header.stamp = rospy.Time.now()
            vis_img_msg.header.frame_id = frame_id
            self.pub_vis_img.publish(vis_img_msg)

    @torch.inference_mode()
    def callback(self, img_msg):
        self.image = self.bridge.imgmsg_to_cv2(img_msg, desired_encoding="rgb8")
        with torch.cuda.amp.autocast(enabled=True):
            image_torch = (
                torch.from_numpy(self.image.transpose(2, 0, 1)).float().to(self.cutie_config.device, non_blocking=True)
                / 255
            )
            prediction = self.predictor.step(image_torch)
            self.mask = torch.max(prediction, dim=0).indices.cpu().numpy().astype(np.uint8)
            self.visualization = overlay_davis(self.image.copy(), self.mask)
            if self.with_bbox and len(np.unique(self.mask)) > 1:
                masks = []
                for i in range(1, len(np.unique(self.mask))):
                    masks.append((self.mask == i).astype(np.uint8))

                self.masks = np.stack(masks, axis=0)
                xyxy = sv.mask_to_xyxy(self.masks)  # [N, 4]
                detections = sv.Detections(
                    xyxy=xyxy,
                    mask=self.masks,
                    tracker_id=np.arange(len(xyxy)),
                )
                box_annotator = sv.BoxAnnotator()
                self.visualization = box_annotator.annotate(
                    scene=self.visualization,
                    detections=detections,
                    labels=[f"ObjectID : {i}" for i in range(len(xyxy))],
                )
        self.publish_result(self.mask, self.visualization, img_msg.header.frame_id)


if __name__ == "__main__":
    rospy.init_node("cutie_node")
    node = CutieNode()
    rospy.spin()
