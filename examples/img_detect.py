#!/usr/bin/env python3
# coding=utf-8
"""YOLOv5 目标检测示例。

使用 YOLOv5 模型对图片进行目标检测，返回检测到的物体类别列表。

依赖: pip install torch torchvision
"""
import argparse

import torch


class ImageDetector:
    def __init__(self, model_path=None):
        if model_path is None:
            # 默认使用 YOLOv5s 预训练模型
            self.model = torch.hub.load("ultralytics/yolov5", "yolov5s")
        else:
            self.model = torch.hub.load("ultralytics/yolov5", "custom", path=model_path)

    def detect_image(self, image):
        """对图片进行目标检测，返回检测到的物体类别集合。"""
        results = self.model(image)
        detected_objects = results.xyxy[0].tolist()

        obj_list = []
        for obj in detected_objects:
            x1, y1, x2, y2, conf, cls = obj
            class_name = results.names[int(cls)]
            obj_list.append(class_name)
            print(f"Detected: {class_name}, Confidence: {conf:.2f}, "
                  f"Coordinates: ({x1:.1f}, {y1:.1f}), ({x2:.1f}, {y2:.1f})")
        return set(obj_list)


def main():
    parser = argparse.ArgumentParser(description="YOLOv5 目标检测示例")
    parser.add_argument("--image", type=str, default="https://ultralytics.com/images/zidane.jpg", help="图片路径或URL")
    parser.add_argument("--model", type=str, default=None, help="自定义模型路径")
    args = parser.parse_args()

    detector = ImageDetector(model_path=args.model)
    obj_list = detector.detect_image(args.image)
    print(f"Detected objects: {obj_list}")


if __name__ == "__main__":
    main()
