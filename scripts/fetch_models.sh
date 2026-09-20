#!/bin/bash
set -e
cd "$(dirname "$0")/../models"
base="https://github.com/opencv/opencv_zoo/raw/main/models"
curl -sL -o face_detection_yunet_2023mar.onnx "$base/face_detection_yunet/face_detection_yunet_2023mar.onnx"
curl -sL -o face_recognition_sface_2021dec.onnx "$base/face_recognition_sface/face_recognition_sface_2021dec.onnx"
ls -la *.onnx
