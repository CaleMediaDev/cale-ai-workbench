# Webcam AI

Open **Webcam AI · People & objects** beneath the camera controls. It uses the
existing raw webcam stream, so no second process opens the device. Each displayed
image and its boxes come from the same inspected frame, with an age indicator.
Capture remains independent of the bounded recognition worker. Closing the panel
stops object inference; turning the camera off clears the view and stops capture.
Animated Cale never supplies raw frames to this panel.

Click a face and choose **Name selected**, or use a configured quick name. With
multiple faces, select the intended face first. Enrollment uses five matching
samples of the selected face; another person taking the seat does not inherit
the name. Recognition requires the existing SFace similarity and runner-up gap.
Uncertain faces remain unknown. Names are taught, not guessed from appearance.

For an explicitly identified newcomer, a one-time `learn_other` request can name
that person automatically beside an already recognized person. Exactly two
separate faces and five consistent newcomer samples are required. A third face,
a missing known face, or a changed newcomer resets the samples. Requests expire
and are removed after enrollment. This is supervised name assignment, not a rule
that labels all strangers with the same name.

NanoDet detects 80 common object classes on the CPU. Click an object and teach a
name to store ORB keypoints/descriptors. Matching requires geometric agreement;
plain, tiny, heavily occluded or changed objects may not match. Teach additional
angles explicitly (up to four examples per name). This does not train a new
general-purpose object class. Tracker numbers describe short-term continuity,
not permanent identities.

Face embeddings, object descriptors and user-supplied names stay in the excluded
local `Media` folder. No video or frame images are saved or uploaded. **Saved
names** provides a forget control with one local previous-version recovery file.
Camera permission retains the app's existing one-hour limit.

The 3.8 MB OpenCV Zoo NanoDet model is downloaded only when this feature is
enabled and verified with SHA-256
`4b82da9944b88577175ee23a459dce2e26e6e4be573def65b1055dc2d9720186`.
Reference preprocessing/decoding and model: [OpenCV Zoo NanoDet](https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_nanodet),
Apache-2.0; license included at `vendor/opencv-nanodet/LICENSE`.
