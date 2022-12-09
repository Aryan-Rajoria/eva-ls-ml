import io
import json
import logging
import os
import random
from urllib.parse import urlparse

import boto3
import cv2
import nest_asyncio
from botocore.exceptions import ClientError
from eva.server.db_api import connect
from label_studio_ml.model import LabelStudioMLBase
from label_studio_ml.utils import DATA_UNDEFINED_NAME
from label_studio_tools.core.utils.io import get_data_dir

logger = logging.getLogger(__name__)

nest_asyncio.apply()
EVA_CURSOR = connect(host='127.0.0.1', port=5432).cursor()

def json_load(file, int_keys=False):
    with io.open(file, encoding='utf8') as f:
        data = json.load(f)
        if int_keys:
            return {int(k): v for k, v in data.items()}
        else:
            return data

class EVAModel(LabelStudioMLBase):
    """
    EVA connection using Label Studio ML backend server. This will allow you to run EVA queries on Label Studio.
    """

    def __init__(self, image_dir=None, labels_file=None, score_threshold=0.3, device='cuda', **kwargs):

        super(EVAModel, self).__init__(**kwargs)

        self.labels_file = labels_file

        UPLOAD_DIR = os.path.join(get_data_dir(), 'media', 'upload')
        self.image_dir = image_dir or UPLOAD_DIR

        # TODO Logging

        if self.labels_file and os.path.exists(self.labels_file):
            self.label_map = json_load(self.labels_file)
        else:
            self.label_map = {}
        
        self.from_name, info = list(self.parsed_label_config.items())[0]
        self.to_name = info['to_name'][0]
        self.value = info['inputs'][0]['value']

        schema = list(self.parsed_label_config.values())[0]

        self.labels_attrs = schema.get('labels_attrs')
        if self.labels_attrs:
            for label_name, label_attrs in self.labels_attrs.items():
                for predicted_value in label_attrs.get('predicted_values', '').split(','):
                    self.label_map[predicted_value] = label_name

        print(schema)
        print(self.label_map)

    def _get_video_size(self, video_path):
        vcap = cv2.VideoCapture(video_path)
        self.width = int(vcap.get(3))
        self.height = int(vcap.get(4))

    def _get_video_url(self, task):
        image_url = task['data'].get(self.value) or task['data'].get(DATA_UNDEFINED_NAME)
        if image_url.startswith('s3://'):
            # presign s3 url
            r = urlparse(image_url, allow_fragments=False)
            bucket_name = r.netloc
            key = r.path.lstrip('/')
            client = boto3.client('s3')
            try:
                image_url = client.generate_presigned_url(
                    ClientMethod='get_object',
                    Params={'Bucket': bucket_name, 'Key': key}
                )
            except ClientError as exc:
                logger.warning(f'Can\'t generate presigned URL for {image_url}. Reason: {exc}')
        return image_url
    
    def get_value_dict(self, bbox, index, label):
        # time is 0.04 per frame
        x1, y1 = bbox[0], bbox[1]
        x2, y2 = bbox[2], bbox[3]
        # print(x1,y1,x2,y2)
        width = ((x2-x1)/self.width)*100
        height = ((y2-y1)/self.height)*100
        x1 = (x1/self.width)*100
        y1 = (y1/self.height)*100
        
        value = {
            'framesCount': 4459,
            'duration': 178.352472,
            'sequence': [
                {
                    'frame': index,
                    'enabled': False,
                    'rotation': 0,
                    'x': x1,
                    'y': y1,
                    'width': width,
                    'height': height,
                    'time': 0.04*index,
                }
            ],
            "labels": [
                label
            ]
        }
        return value

    def eva_to_ls(self, result_df):
        result = []
        count=1
        for index, row in result_df.iterrows():
            
            #objects in a scene
            num = len(row['fastrcnnobjectdetector.labels'])
            for i in range(num):
                bbox = row['fastrcnnobjectdetector.bboxes'][i]
                label = row['fastrcnnobjectdetector.labels'][i]
                val = self.get_value_dict(bbox, count, label)
                id_gen = random.randrange(10**10)
                result.append({
                    'value': val,
                    'id': str(id_gen),
                    'from_name': "box",
                    'to_name': "video",
                    'type': 'videorectangle',
                    'origin': 'manual'
                })
            count+=1
        return result

    def for_now_ingest_eva(self, video_path):
        EVA_CURSOR.execute('drop table OneVideo')
        result = EVA_CURSOR.fetch_all()
        EVA_CURSOR.execute(f'LOAD FILE "{video_path}" INTO OneVideo')
        result = EVA_CURSOR.fetch_all()
        print(result)

    def eva_result(self, video_path):
        "The function uses SELECT statement to fetch results from eva DB"
        # TODO Get a way to get TASK_ID from
        # For now assuming v1

        EVA_CURSOR.execute("""SELECT id, FastRCNNObjectDetector(data) 
                  FROM v3 WHERE id<10;
        """)
        result_dataframe = EVA_CURSOR.fetch_all().batch.frames
        print(result_dataframe)

        return result_dataframe

    def predict(self, tasks, **kwargs):

        task = tasks[0]
        video_url = self._get_video_url(task)
        # video_path = self.get_local_path(video_url)
        video_path = "/" + tasks[0]['data']['video'].split('?d=')[-1]
        self.for_now_ingest_eva(video_path)
        self._get_video_size(video_path)

        model_results = self.eva_result(video_path)

        output = self.eva_to_ls(model_results)
        predictions = [
            {
                "result": output
            }
        ]
        # print(predictions)
        return predictions
