import torch


class ImageDetector:
    def __init__(self):
        self.model = torch.hub.load("ultralytics/yolov5", "custom", path="/Users/gatilin/PycharmProjects/smart-spider/yolov5m_Objects365.pt")
    def detect_image(self, image):
        # 进行推理
        results = self.model(image)
        print("results:", results)
        # 显示结果
        # results.show()

        # 获取检测到的对象和类别
        detected_objects = results.xyxy[0].tolist()

        obj_list = []
        # 打印对象和类别
        for obj in detected_objects:
            x1, y1, x2, y2, conf, cls = obj
            class_name = results.names[int(cls)]
            obj_list.append(class_name)
            print(f"Detected object: {class_name}, Confidence: {conf:.2f}, Coordinates: ({x1:.1f}, {y1:.1f}), ({x2:.1f}, {y2:.1f})")
        return set(obj_list)


if __name__ == '__main__':
    img_detector = ImageDetector()
    img = "https://ultralytics.com/images/zidane.jpg"
    obj_list = img_detector.detect_image(img)
    print("obj_list:", obj_list)