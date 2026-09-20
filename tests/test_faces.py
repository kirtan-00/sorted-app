import os, numpy as np, pytest
from PIL import Image
from photosort.faces import FaceEngine, Face

def test_no_faces_on_noise(make_img):
    eng = FaceEngine()
    assert eng.detect(Image.open(make_img(name="n.jpg"))) == []

@pytest.mark.skipif(not os.environ.get("PHOTOSORT_FACE_FIXTURE"), reason="needs a real face photo")
def test_real_face():
    from photosort.decode import load_preview
    eng = FaceEngine()
    faces = eng.detect(load_preview(os.environ["PHOTOSORT_FACE_FIXTURE"]))
    assert faces and isinstance(faces[0], Face)
    assert faces[0].embed.shape == (128,) and abs(np.linalg.norm(faces[0].embed) - 1) < 1e-3
    assert faces[0].eye_sharp >= 0

def test_face_eq_does_not_raise():
    a = Face(0, 0, 1, 1, 0.9, np.zeros((5, 2)), np.zeros(128, np.float32), 0.0)
    b = Face(0, 0, 1, 1, 0.9, np.zeros((5, 2)), np.zeros(128, np.float32), 0.0)
    assert (a == b) is False and a == a and a != b
