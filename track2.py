import sys
sys.path.insert(0, './yolov5')

from yolov5.utils.google_utils import attempt_download
from yolov5.models.experimental import attempt_load
from yolov5.utils.datasets import LoadImages, LoadStreams
from yolov5.utils.general import check_img_size, non_max_suppression, scale_coords, \
    check_imshow
from yolov5.utils.torch_utils import select_device, time_synchronized
from deep_sort_pytorch.utils.parser import get_config
from deep_sort_pytorch.deep_sort import DeepSort
import argparse
import os
import platform
import shutil
import time
from pathlib import Path
import cv2
import torch
import torch.backends.cudnn as cudnn
import multiprocessing



palette = (2 ** 11 - 1, 2 ** 15 - 1, 2 ** 20 - 1)


def xyxy_to_xywh(*xyxy):
    """" Calculates the relative bounding box from absolute pixel values. """
    bbox_left = min([xyxy[0].item(), xyxy[2].item()])
    bbox_top = min([xyxy[1].item(), xyxy[3].item()])
    bbox_w = abs(xyxy[0].item() - xyxy[2].item())
    bbox_h = abs(xyxy[1].item() - xyxy[3].item())
    x_c = (bbox_left + bbox_w / 2)
    y_c = (bbox_top + bbox_h / 2)
    w = bbox_w
    h = bbox_h
    return x_c, y_c, w, h

def xyxy_to_tlwh(bbox_xyxy):
    tlwh_bboxs = []
    for i, box in enumerate(bbox_xyxy):
        x1, y1, x2, y2 = [int(i) for i in box]
        top = x1
        left = y1
        w = int(x2 - x1)
        h = int(y2 - y1)
        tlwh_obj = [top, left, w, h]
        tlwh_bboxs.append(tlwh_obj)
    return tlwh_bboxs


def compute_color_for_labels(label):
    """
    Simple function that adds fixed color depending on the class
    """
    color = [int((p * (label ** 2 - label + 1)) % 255) for p in palette]
    return tuple(color)


def draw_boxes(img, bbox, identities=None, offset=(0, 0)):
    for i, box in enumerate(bbox):
        x1, y1, x2, y2 = [int(i) for i in box]
        x1 += offset[0]
        x2 += offset[0]
        y1 += offset[1]
        y2 += offset[1]
        # box text and bar
        id = int(identities[i]) if identities is not None else 0
        color = compute_color_for_labels(id)
        label = '{}{:d}'.format("", id)
        t_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_PLAIN, 2, 2)[0]
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 3)
        cv2.rectangle(
            img, (x1, y1), (x1 + t_size[0] + 3, y1 + t_size[1] + 4), color, -1)
        cv2.putText(img, label, (x1, y1 +
                                 t_size[1] + 4), cv2.FONT_HERSHEY_PLAIN, 2, [255, 255, 255], 2)
    return img


def tracker_process(input_queue, output_queue):
    trackers = {}
    while True:
        data = input_queue.get()
        if data is not None:
            if data['type'] == 'create_tracker':
                tracker = cv2.TrackerKCF_create()
                tracker.init(data['frame'], data['init_bbox'])
                trackers[data['id']] = { 'tracker': tracker }
            elif data['type'] == 'get_tracker_count':
                output_queue.put({'tracker_count': len(trackers)})
            elif data['type'] == 'update_tracker':
                output = {'type': 'update_tracker', 'data': {}}
                for tracker_id, tracker in trackers.items():
                    output['data'][tracker_id] = {}
                    track_ok, track_bbox = trackers[tracker_id]['tracker'].update(data['frame'])
                    if track_ok:
                        # Tracking success
                        output['data'][tracker_id]['track_ok'] = True
                        output['data'][tracker_id]['updated_bbox'] = [int(v) for v in track_bbox]
                    else:
                        output['data'][tracker_id]['track_ok'] = False
                output_queue.put(output)
            elif data['type'] == 'remove_tracker':
                if data['id'] in trackers:
                    del trackers[data['id']]
            else:
                raise Exception('unknown data from parent process')

def detect(opt):
    OBJECT_DETECT_DELAY = 0.5
    # OBJECT_DETECT_CONFIDENCE_THRESHOLD = 0.7
    TRACKER_UPDATE_DELAY = 0
    TRACKER_FAIL_COUNT_THRESHOLD = 20
    COUNT_RIGHT = True
    INTERSECT_DELAY = 0.5 # if object passed counting line, it can't be counted again until INTERSECT_DELAY seconds passed

    fps = 0

    #
    total_passed_objects = 0
    vacant_tracker_id = 1
    last_detect_tick_count = None
    last_tracker_update_tick_count = None
    POOL_COUNT = multiprocessing.cpu_count() - 1
    tracker_data_list = {}
    input_queues = [multiprocessing.Queue() for x in range(POOL_COUNT) ]
    output_queues = [multiprocessing.Queue() for x in range(POOL_COUNT) ] 
    for i in range(POOL_COUNT):
        p = multiprocessing.Process(target=tracker_process, args=(input_queues[i], output_queues[i], ))
        p.daemon = True
        p.start()

    #
    out, source, yolo_weights, deep_sort_weights, show_vid, save_vid, save_txt, imgsz, evaluate = \
        opt.output, opt.source, opt.yolo_weights, opt.deep_sort_weights, opt.show_vid, opt.save_vid, \
            opt.save_txt, opt.img_size, opt.evaluate
    webcam = source == '0' or source.startswith(
        'rtsp') or source.startswith('http') or source.endswith('.txt')

    # initialize deepsort
    # cfg = get_config()
    # cfg.merge_from_file(opt.config_deepsort)
    # attempt_download(deep_sort_weights, repo='mikel-brostrom/Yolov5_DeepSort_Pytorch')
    # deepsort = DeepSort(cfg.DEEPSORT.REID_CKPT,
    #                     max_dist=cfg.DEEPSORT.MAX_DIST, min_confidence=cfg.DEEPSORT.MIN_CONFIDENCE,
    #                     nms_max_overlap=cfg.DEEPSORT.NMS_MAX_OVERLAP, max_iou_distance=cfg.DEEPSORT.MAX_IOU_DISTANCE,
    #                     max_age=cfg.DEEPSORT.MAX_AGE, n_init=cfg.DEEPSORT.N_INIT, nn_budget=cfg.DEEPSORT.NN_BUDGET,
    #                     use_cuda=True)

    # Initialize
    device = select_device(opt.device)

    # The MOT16 evaluation runs multiple inference streams in parallel, each one writing to
    # its own .txt file. Hence, in that case, the output folder is not restored
    if not evaluate:
        if os.path.exists(out):
            pass
            shutil.rmtree(out)  # delete output folder
        os.makedirs(out)  # make new output folder
    half = device.type != 'cpu'  # half precision only supported on CUDA

    # Load model
    model = attempt_load(yolo_weights, map_location=device)  # load FP32 model
    stride = int(model.stride.max())  # model stride
    imgsz = check_img_size(imgsz, s=stride)  # check img_size
    names = model.module.names if hasattr(model, 'module') else model.names  # get class names
    if half:
        model.half()  # to FP16

    # Set Dataloader
    vid_path, vid_writer = None, None
    # Check if environment supports image displays
    if show_vid:
        show_vid = check_imshow()

    if webcam:
        cudnn.benchmark = True  # set True to speed up constant image size inference
        dataset = LoadStreams(source, img_size=imgsz, stride=stride)
    else:
        dataset = LoadImages(source, img_size=imgsz)

    # Get names and colors
    names = model.module.names if hasattr(model, 'module') else model.names

    # Run inference
    if device.type != 'cpu':
        model(torch.zeros(1, 3, imgsz, imgsz).to(device).type_as(next(model.parameters())))  # run once
    t0 = time.time()

    # save_path = str(Path(out))
    # extract what is in between the last '/' and last '.'
    # txt_file_name = source.split('/')[-1].split('.')[0]
    # txt_path = str(Path(out)) + '/' + txt_file_name + '.txt'

    for frame_idx, (path, img, im0s, vid_cap) in enumerate(dataset):
        tick_count = cv2.getTickCount()
        tick_freq = cv2.getTickFrequency()

        img = torch.from_numpy(img).to(device)
        img = img.half() if half else img.float()  # uint8 to fp16/32
        img /= 255.0  # 0 - 255 to 0.0 - 1.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
            
        if webcam:  # batch_size >= 1
            im0 = im0s[0].copy()
        else:
            im0 = im0s

        rows = im0.shape[0]
        cols = im0.shape[1]

        if last_detect_tick_count == None or (tick_count - last_detect_tick_count) / tick_freq > OBJECT_DETECT_DELAY:
            last_detect_tick_count = tick_count

            # Inference
            pred = model(img, augment=opt.augment)[0]

            # Apply NMS
            pred = non_max_suppression(
                pred, opt.conf_thres, opt.iou_thres, classes=opt.classes, agnostic=opt.agnostic_nms)

            # Process detections
            for i, det in enumerate(pred):  # detections per image
                if webcam:  # batch_size >= 1
                    p, s = path[i], '%g: ' % i
                else:
                    p, s = path, ''

           

                # s += '%gx%g ' % img.shape[2:]  # print string
                # save_path = str(Path(out) / Path(p).name)
                # print(det)
                # print(len(det))

                if det is not None and len(det):
                    # Rescale boxes from img_size to im0 size
                    det[:, :4] = scale_coords(
                        img.shape[2:], det[:, :4], im0.shape).round()

                    # Print results
                    for c in det[:, -1].unique():
                        n = (det[:, -1] == c).sum()  # detections per class
                        s += '%g %ss, ' % (n, names[int(c)])  # add to string


                    xywh_bboxs = []
                    # confs = []

                    # Adapt detections to deep sort input format
                    for *xyxy, conf, cls in det:
                        # to deep sort format
                        # x_c, y_c, bbox_w, bbox_h = xyxy_to_xywh(*xyxy)
                        # xywh_obj = [x_c, y_c, bbox_w, bbox_h]
                        # xywh_bboxs.append(xywh_obj)
                        # confs.append([conf.item()])
                        # print(xyxy)

                        # for i in range(len(xywh_bboxs)):
                        x = int(xyxy[0])
                        y = int(xyxy[1])
                        right = int(xyxy[2])
                        bottom = int(xyxy[3])
                        # area = round((right - x) * (bottom - y) / 1000)
                        # aspect_ratio = round((right - x) / (bottom - y), 2)
                        cv2.rectangle(im0, (x, y), (right, bottom), (0, 255, 255), thickness=2)
                        cv2.putText(im0, '', (x, y-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
                        # cv2.putText(im0,  str(area) + ' / ' + str(aspect_ratio) , (x, y-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
                        # if name == 'person' :
                        center_x = int((x + right) / 2)
                        center_y = int((y + bottom) / 2)

                        # check if there's already a tracker here
                        intersect_existing_trackeers = False
                        for tracker_id, tracker_data in tracker_data_list.items():
                            (x_t, y_t, w_t, h_t) = tracker_data['bbox']
                            if center_x > x_t and center_x < x_t + w_t and center_y > y_t and center_y < y_t + h_t:
                                intersect_existing_trackeers = True
                                break

                        # add new tracker for person
                        if not intersect_existing_trackeers:
                            track_bbox = (x, y, right - x, bottom - y)

                            # find least busy process
                            for iq in input_queues:
                                iq.put({'type': 'get_tracker_count'})
                            process_data = []
                            for j in range(len(output_queues)):
                                d = output_queues[j].get()
                                if 'tracker_count' in d:
                                    process_data.append({'idx': j, 'tracker_count': d['tracker_count']})
                                else:
                                    raise Exception('Subprocess wrong answer')
                            process_data = sorted(process_data, key=lambda k: k['tracker_count']) 
                            least_busy_process_idx = process_data[0]['idx']
                            
                            # find vacant tracking ID
                            # vacant_tracker_id = vacant_tracker_id + 1
                            vacant_tracker_id = 0
                            while True:
                                if vacant_tracker_id in tracker_data_list:
                                    vacant_tracker_id += 1
                                    continue
                                else:
                                    break

                            # add tracking data to main process and subprocess
                            tracker_data_list[vacant_tracker_id] = {'bbox': track_bbox, 'last_bbox': track_bbox, 'fail_count': 0, 'last_intersect_tick_count': 0}
                            input_queues[least_busy_process_idx].put({'type': 'create_tracker', 'frame': im0, 'init_bbox': track_bbox, 'id': vacant_tracker_id })


         # Update tracker
        if last_tracker_update_tick_count == None or (tick_count - last_tracker_update_tick_count) / tick_freq > TRACKER_UPDATE_DELAY:
            last_tracker_update_tick_count = tick_count

            for i in range(len(input_queues)):
                input_queues[i].put({'type':'update_tracker', 'frame': im0})

            for i in range(len(output_queues)):
                # TODO timeout?

                # id, bbox, last_bbox, fail_count
                res = output_queues[i].get()
                if 'type' in res and res['type'] == 'update_tracker':
                    for key_id, track_result in res['data'].items():
                        if key_id in tracker_data_list:
                            if track_result['track_ok'] is True:
                                tracker_data_list[key_id]['fail_count'] = 0
                                tracker_data_list[key_id]['last_bbox'] = tracker_data_list[key_id]['bbox'] 
                                tracker_data_list[key_id]['bbox'] = track_result['updated_bbox']
                            else: 
                                tracker_data_list[key_id]['fail_count'] = tracker_data_list[key_id]['fail_count'] + 1
                                
                        else:
                            raise Exception('Subprocess have tracking ID not tracked by main process')
                else:
                    raise Exception('Subprocess wrong answer')

        # Remove failed tracker
        for tracker_id in list(tracker_data_list):
            if tracker_data_list[tracker_id]['fail_count'] > TRACKER_FAIL_COUNT_THRESHOLD:
                for iq in input_queues:
                    iq.put({'type': 'remove_tracker', 'id': tracker_id})
                del tracker_data_list[tracker_id]


        # Render tracker
        for tracker_id, tracker_data in tracker_data_list.items():
            (x, y, w, h) = tracker_data['bbox']
            if tracker_data['fail_count'] > 0:
                color = (0,0,255)
            else:
                color = (0,255,0)

                # Detect if track box intersect center of screen
                (prev_x, prev_y, prev_w, prev_h) = tracker_data['last_bbox']
                center_x = cols/2
                intersects_center = center_x > x and center_x < x + w
                prev_intersects_center = center_x > prev_x and center_x < prev_x + prev_w
                in_intersect_delay = (tick_count - tracker_data['last_intersect_tick_count']) / tick_freq < INTERSECT_DELAY
                
                if intersects_center:
                    tracker_data['last_intersect_tick_count'] = tick_count
                    if not in_intersect_delay and not prev_intersects_center:
                        if (x + w/2) > (prev_x + prev_w/2):
                            total_passed_objects += 1 if COUNT_RIGHT else -1
                        else:
                            total_passed_objects += -1 if COUNT_RIGHT else 1
                
                if in_intersect_delay:
                    color = (255,0,0)

            cv2.rectangle(im0, (x, y), (x + w, y + h), color, thickness=2)
            cv2.putText(im0, str(tracker_id), (int(x + 5) , int(y + 40)), cv2.FONT_HERSHEY_SIMPLEX, 1.5, color, 2)




            # if det is not None and len(det):
            #     # Rescale boxes from img_size to im0 size
            #     det[:, :4] = scale_coords(
            #         img.shape[2:], det[:, :4], im0.shape).round()



            #     # Print results
            #     for c in det[:, -1].unique():
            #         n = (det[:, -1] == c).sum()  # detections per class
            #         s += '%g %ss, ' % (n, names[int(c)])  # add to string




            #     xywh_bboxs = []
            #     # confs = []

            #     # Adapt detections to deep sort input format
            #     for *xyxy, conf, cls in det:
            #         # to deep sort format
            #         x_c, y_c, bbox_w, bbox_h = xyxy_to_xywh(*xyxy)
            #         xywh_obj = [x_c, y_c, bbox_w, bbox_h]
            #         xywh_bboxs.append(xywh_obj)
            #         # confs.append([conf.item()])

            #     print(xywh_bboxs)
                # xywhs = torch.Tensor(xywh_bboxs)
                # confss = torch.Tensor(confs)

                # # pass detections to deepsort
                # outputs = deepsort.update(xywhs, confss, im0)

                # # draw boxes for visualization
                # if len(outputs) > 0:
                #     bbox_xyxy = outputs[:, :4]
                #     identities = outputs[:, -1]
                #     draw_boxes(im0, bbox_xyxy, identities)
                    
                    # # to MOT format
                    # tlwh_bboxs = xyxy_to_tlwh(bbox_xyxy)

                    # # Write MOT compliant results to file
                    # if save_txt:
                    #     for j, (tlwh_bbox, output) in enumerate(zip(tlwh_bboxs, outputs)):
                    #         bbox_top = tlwh_bbox[0]
                    #         bbox_left = tlwh_bbox[1]
                    #         bbox_w = tlwh_bbox[2]
                    #         bbox_h = tlwh_bbox[3]
                    #         identity = output[-1]
                    #         with open(txt_path, 'a') as f:
                    #             f.write(('%g ' * 10 + '\n') % (frame_idx, identity, bbox_top,
                    #                                         bbox_left, bbox_w, bbox_h, -1, -1, -1, -1))  # label format

            # else:
            #     deepsort.increment_ages()

            # Print time (inference + NMS)
            # print('%sDone. (%.3fs)' % (s, t2 - t1))

        # Stream results
        if show_vid:
            cv2.putText(im0, "FPS : " + str(int(fps)), (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1);
            cv2.imshow(p, cv2.resize(im0, None, None, fx=2, fy=2))
            fps = tick_freq / (cv2.getTickCount() - tick_count);

            if cv2.waitKey(1) == ord('q'):  # q to quit
                raise StopIteration

            # Save results (image with detections)
            # if save_vid:
            #     if vid_path != save_path:  # new video
            #         vid_path = save_path
            #         if isinstance(vid_writer, cv2.VideoWriter):
            #             vid_writer.release()  # release previous video writer
            #         if vid_cap:  # video
            #             fps = vid_cap.get(cv2.CAP_PROP_FPS)
            #             w = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            #             h = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            #         else:  # stream
            #             fps, w, h = 30, im0.shape[1], im0.shape[0]
            #             save_path += '.mp4'

            #         vid_writer = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (w, h))
            #     vid_writer.write(im0)

    # if save_txt or save_vid:
    #     print('Results saved to %s' % os.getcwd() + os.sep + out)
    #     if platform == 'darwin':  # MacOS
    #         os.system('open ' + save_path)

    print('Done')
    pool.terminate()
    cv2.destroyAllWindows()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--yolo_weights', type=str, default='yolov5/weights/crowdhuman_yolov5m.pt', help='model.pt path')
    parser.add_argument('--deep_sort_weights', type=str, default='deep_sort_pytorch/deep_sort/deep/checkpoint/ckpt.t7', help='ckpt.t7 path')
    # file/folder, 0 for webcam
    parser.add_argument('--source', type=str, default='0', help='source')
    parser.add_argument('--output', type=str, default='inference/output', help='output folder')  # output folder
    parser.add_argument('--img-size', type=int, default=640, help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float, default=0.7, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.5, help='IOU threshold for NMS')
    parser.add_argument('--fourcc', type=str, default='mp4v', help='output video codec (verify ffmpeg support)')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--show-vid', action='store_true', help='display tracking video results')
    parser.add_argument('--save-vid', action='store_true', help='save video tracking results')
    parser.add_argument('--save-txt', action='store_true', help='save MOT compliant results to *.txt')
    # class 0 is person, 1 is bycicle, 2 is car... 79 is oven
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --class 0, or --class 16 17')
    parser.add_argument('--agnostic-nms', action='store_true', help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--evaluate', action='store_true', help='augmented inference')
    parser.add_argument("--config_deepsort", type=str, default="deep_sort_pytorch/configs/deep_sort.yaml")
    args = parser.parse_args()
    args.img_size = check_img_size(args.img_size)

    with torch.no_grad():
        detect(args)

# python track.py --source 0 --show-vid --classes 0