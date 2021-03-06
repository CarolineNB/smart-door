from __future__ import print_function
import base64
import boto3
from botocore.exceptions import ClientError
import sys
import cv2
import json
# from random import randint
import uuid
import datetime
import time
import random

COLLECTION = 'faces'  # Rekognition collection
REGION = 'us-east-1'
BUCKET = 'smart-door-image-store'
EXPIRY_5 = 60 * 5
OWNER_PHONE_NUMBER = '+13473563326'
OWNER_URL = "https://d2gzbq8f8019ge.cloudfront.net/wp1.html"
VISITOR_URL = "https://d2gzbq8f8019ge.cloudfront.net/wp2.html"

s3_client = boto3.client('s3')
sns_client = boto3.client('sns')
s3_resource = boto3.resource('s3')
kvs_video_client = boto3.client('kinesisvideo')
rekognition = boto3.client("rekognition", REGION)
dynamo_resource = boto3.resource('dynamodb')
dynamo_visitors_table = dynamo_resource.Table("visitors")
dynamo_passcodes_table = dynamo_resource.Table('passcodes')


def index_faces(key, ExternalImageId, attributes=()):
    response = rekognition.index_faces(
        Image={
            "S3Object": {
                "Bucket": BUCKET,
                "Name": key,
            }
        },
        CollectionId=COLLECTION,
        ExternalImageId=ExternalImageId,
        DetectionAttributes=attributes,
    )
    try:
        return response['FaceRecords'][0]['Face']['FaceId']
    except:
        # print("index_faces() response:" + str(response))
        return None


def get_payload_from_event(event):
    # for record in event['Records'] # I think we can just use event['Records'][0]
    # Kinesis data is base64 encoded so decode here
    byte_str = base64.b64decode(event['Records'][0]["kinesis"]["data"])  # of type bytes
    dict_payload = json.loads(byte_str.decode("UTF-8"))
    return dict_payload


# Get an endpoint so that we can send the GET_MEDIA request to it later
def get_get_media_endpoint():
    return kvs_video_client.get_data_endpoint(
        StreamName="KVS1",
        APIName="GET_MEDIA"
    )


# Now that we have the endpoint for GET_MEDIA, we can start a session
# and pass the endpoint_url
def start_kvs_session(endpoint_response):
    return boto3.client(
        'kinesis-video-media',
        endpoint_url=endpoint_response["DataEndpoint"]
    )


# Now that we started a session, we can finally call GET_MEDIA
def get_media_by_fragment_number(fragment_number, kvs_video_media_client):
    return kvs_video_media_client.get_media(
        StreamName="KVS1",
        StartSelector={
            # 'StartSelectorType': 'FRAGMENT_NUMBER',
            'StartSelectorType': 'NOW',
            # 'AfterFragmentNumber': payload['InputInformation']['KinesisVideo']['FragmentNumber']
            # 'AfterFragmentNumber': fragment_number
        }
    )


# Get GET_MEDIA API endpoint, start a sesh, and get the media via the endpoint
def get_media_stream(payload):
    endpoint_response = get_get_media_endpoint()
    kvs_video_media_client = start_kvs_session(endpoint_response)
    fragment_number = payload['InputInformation']['KinesisVideo']['FragmentNumber']
    kvs_stream = get_media_by_fragment_number(fragment_number, kvs_video_media_client)
    return kvs_stream


def get_image_from_stream(payload):
    kvs_stream = get_media_stream(payload)
    clip = kvs_stream['Payload'].read()  # Get the video clip of the payload
    img_temp_location = '/tmp/img_frame.jpg'
    vid_temp_location = '/tmp/stream.avi'

    with open(vid_temp_location, 'wb') as f:
        # First need to write the clip to a file so we
        # can later extract a frame from it
        f.write(clip)
        vidcap = cv2.VideoCapture(vid_temp_location)  # Capture an img from it
        vidCapSuccess, image = vidcap.read()
        if vidCapSuccess is False:
            print("Vidcap problem")
            exit(1)
        writeStatusSuccess = cv2.imwrite(img_temp_location, image)  # Save image to file
        if writeStatusSuccess is False:
            print("Image write problem")
            exit(1)
        return img_temp_location


# Store face in s3 bucket and return ExternalImageId
def upload_visitor_image_to_s3(visitor_image_local_path, ExternalImageId):
    unique_img_id = str(uuid.uuid4())  # Generate a unique identifier for this image
    # Upload the file to S3
    object_key = ExternalImageId + '/' + unique_img_id + '.jpg'
    try:
        response = s3_client.upload_file(visitor_image_local_path, BUCKET, object_key)
        return object_key
    except ClientError as e:
        logging.error(e)
    return object_key


def upload_unknown_visitor_image_to_s3(visitor_image_local_path, ExternalImageId):
    # Upload the file to S3
    object_key = ExternalImageId
    try:
        response = s3_client.upload_file(visitor_image_local_path, BUCKET, object_key)
        return object_key
    except ClientError as e:
        logging.error(e)
    return object_key


# Udpate visitor information with new image
def update_visitor(visitor, s3_object_key):
    external_image_id = visitor['Item']['ExternalImageId']
    name = visitor['Item']['name']
    phone_number = visitor['Item']['phoneNumber']

    new_photo = {
        'objectKey': s3_object_key,
        'bucket': BUCKET,
        'createdTimestamp': datetime.datetime.now().isoformat(timespec='seconds')
    }
    photos = visitor['Item']['photos']
    photos.append(new_photo)

    visitor = {
        'ExternalImageId': external_image_id,
        'name': name,
        'phoneNumber': phone_number,
        'photos': photos
    }
    dynamo_visitors_table.put_item(Item=visitor)


# store otp and expiration for known visitor
def store_otp(otp, phone_number, range=EXPIRY_5):
    password = {
        'PhoneNumber': phone_number,
        'OTP': otp,
        'ExpTime': int(int(time.time()) + range)
    }
    dynamo_passcodes_table.put_item(Item=password)


# send SMS with OTP if it's a known visitor
def send_sms_to_known_visitor(otp, phone_number, externalID):
    message = "Welcome back! Here is your one time password: \"" + otp + "\". " + "This password will expire in 5 minutes. Please enter it on this webpage: " + VISITOR_URL + "?" + "externalID=" + externalID + ". Note: If you received multiple OTPs, please use the one from the most recent text."
    sns_client.publish(PhoneNumber=phone_number, Message=message)


# send SMS requesting access if it's an unknown visitor
# def send_review_to_owner(ExternalImageId, FaceId, s3_object_key):
def send_review_to_owner():
    # TODO: update with group member's phone
    phone_number = OWNER_PHONE_NUMBER  # Hardcoded for now. Maybe we add a DB entry in the future
    # include face and file ID
    # visitor_verification_link = "https://smart-door-b1.s3.amazonaws.com/wp1.html" + "?" + "ExternalImageId=" + ExternalImageId + "&S3ObjKey=" + s3_object_key + "&FaceId=" + FaceId
    # TODO: make sure format of variable in URL matches LF0
    message = "Hello, you have received a visitor verification request. To see who is at your door and admit/deny them access, click here: " + OWNER_URL
    sns_client.publish(PhoneNumber=phone_number, Message=message)


def is_known_visitor(dict_payload):
    return len(dict_payload['FaceSearchResponse'][0]['MatchedFaces']) > 0


def get_ExternalImageId(dict_payload):
    return dict_payload['FaceSearchResponse'][0]['MatchedFaces'][0]['Face']['ExternalImageId']


def lambda_handler(event, context):
    payload = get_payload_from_event(event)  # Decode the event record
    print(payload)
    visitor_image_local_path = get_image_from_stream(payload)

    ''' If known visitor, do the following:
        1. Store to s3 in visitor's folder:  s3://<ExternalImageId>/<newImg>
        2. Index in Rekognition
        3. Append s3 photo object key to visitors table photos array
        4. Send OTP to visitor
    '''
    if is_known_visitor(payload):
        ExternalImageId = get_ExternalImageId(payload)

        s3_object_key = upload_visitor_image_to_s3(visitor_image_local_path, ExternalImageId)

        # (try to) Index new image of known visitor to train model
        if not index_faces(s3_object_key, ExternalImageId):
            print("Error: Couldn't index face")
            exit(1)

        # Return visitor information by finding photoID key in visitor table
        visitor = dynamo_visitors_table.get_item(Key={'ExternalImageId': ExternalImageId})

        # append faceId in visitor dynamoDB object list
        update_visitor(visitor, s3_object_key)

        # add password and expiriation to password dynamo
        phone_number = visitor['Item']['phoneNumber']

        # create and store OTP in passwords table
        otp = str(random.randint(100001, 999999))
        store_otp(otp, phone_number)

        print("abouta send a text to the visitor")
        # Send sms to returning visitor
        send_sms_to_known_visitor(otp, phone_number, ExternalImageId)

    # Else, send visitor info to owner for review
    else:
        # Use a constant name so that if this gets triggered multiple times,
        # we won't write a bunch of different image
        ExternalImageId = 'current-visitor.jpg'
        s3_object_key = upload_unknown_visitor_image_to_s3(visitor_image_local_path, ExternalImageId)

        # (try to) Index new image of unknown visitor
        # if not index_faces(s3_object_key, ExternalImageId):
        #    print("Error: Couldn't index face")
        #    exit(1)
        # FaceId = index_faces(s3_object_key, ExternalImageId)
        # FaceId = "abc"

        # store new face in visitors table
        # send_review_to_owner(ExternalImageId, FaceId, s3_object_key)
        send_review_to_owner()

    return {
        'statusCode': 200,
        'body': json.dumps('LF1 success!')
    }