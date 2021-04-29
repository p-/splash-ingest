from os import access
import h5py
import json
import sys
from datetime import datetime
import hashlib
import urllib
import base64
import logging
from pathlib import Path
from pprint import pprint

import numpy as np
import requests  # for HTTP requests

from splash_ingest.docstream import MappedH5Generator
from splash_ingest.model import Mapping, Issue

logger = logging.getLogger("splash" + __name__)
can_debug = False
can_info = False


def debug(message):
    if can_debug:
        logger.debug(message)


def info(message):
    if can_info:
        logger.info(message)


class ScicatIngestor():
    # settables
    host = "localhost:3000"
    baseurl = "http://" + host + "/api/v3/"
    # timeouts = (4, 8)  # we are hitting a transmission timeout...
    timeouts = None  # we are hitting a transmission timeout...
    sslVerify = True  # do not check certificate
    username = "ingestor"  # default username
    password = "aman"     # default password
    delete_existing = False
    # You should see a nice, but abbreviated table here with the logbook contents.
    token = None  # store token here
    settables = ['host', 'baseurl', 'timeouts', 'sslVerify', 'username', 'password', 'token']
    pid = 0  # gets set if you search for something
    entries = None  # gets set if you search for something
    datasetType = "RawDatasets"
    datasetTypes = ["RawDatasets", "DerivedDatasets", "Proposals"]
    test = False

    def __init__(self, issues: list[Issue], **kwargs):
        # nothing to do
        for key, value in kwargs.items():
            assert key in self.settables, f"key {key} is not a valid input argument"
            setattr(self, key, value)
        # get token
        self.token = self._get_token(username=self.username, password=self.password)

    def _get_token(self, username=None, password=None):
        if username is None:
            username = self.username
        if password is None:
            password = self.password
        """logs in using the provided username / password combination 
        and receives token for further communication use"""
        logger.info("Getting new token ...")

        response = requests.post(
            self.baseurl + "Users/login",
            json={"username": username, "password": password},
            timeout=self.timeouts,
            stream=False,
            verify=self.sslVerify,
        )
        if not response.ok:
            logger.error(f'** Error received: {response}')
            err = response.json()["error"]
            logger.error(f'{err["name"]}, {err["statusCode"]}: {err["message"]}')
            self._add_issue(f'error getting token {err["name"]}, {err["statusCode"]}: {err["message"]}')

        data = response.json()
        # print("Response:", data)
        token = data["id"]  # not sure if semantically correct
        logger.info(f"token: {token}")
        self.token = token  # store new token
        return token

    def _send_to_scicat(self, url, dataDict=None, cmd="post"):
        """ sends a command to the SciCat API server using url and token, returns the response JSON
        Get token with the getToken method"""
        if cmd == "post":
            response = requests.post(
                url,
                params={"access_token": self.token},
                json=dataDict,
                timeout=self.timeouts,
                stream=False,
                verify=self.sslVerify,
            )
        elif cmd == "delete":
            response = requests.delete(
                url, params={"access_token": self.token}, 
                timeout=self.timeouts, 
                stream=False,
                verify=self.sslVerify,
            )
        elif cmd == "get":
            response = requests.get(
                url,
                params={"access_token": self.token},
                json=dataDict,
                timeout=self.timeouts,
                stream=False,
                verify=self.sslVerify,
            )
        elif cmd == "patch":
            response = requests.patch(
                url,
                params={"access_token": self.token},
                json=dataDict,
                timeout=self.timeouts,
                stream=False,
                verify=self.sslVerify,
            )
        return response

    def _get_file_size(self, pathobj):
        filesize = pathobj.lstat().st_size
        return filesize

    def _get_checksum(self, pathobj):
        with open(pathobj) as file_to_check:
            # pipe contents of the file through
            return hashlib.md5(file_to_check.read()).hexdigest()

    def _add_thumbnail(self, dataset_id=None, filename=None, dataset_type="RawDatasets"):

        def encodeImageToThumbnail(filename, imType='jpg'):
            header = "data:image/{imType};base64,".format(imType=imType)
            with open(filename, 'rb') as f:
                data = f.read()
            dataBytes = base64.b64encode(data)
            dataStr = dataBytes.decode('UTF-8')
            return header + dataStr

        dataBlock = {
            "caption": filename.stem,
            "thumbnail": encodeImageToThumbnail(filename),
            "datasetId": dataset_id,
            "ownerGroup": "BAM 6.5",
        }

        url = self.baseurl + f"{dataset_type}/{urllib.parse.quote_plus(dataset_id)}/attachments"
        logger.debug(url)
        resp = requests.post(
                    url,
                    params={"access_token": self.token},
                    timeout=self.timeouts,
                    stream=False,
                    json=dataBlock,
                    verify=self.sslVerify,
                )
        return resp

    def _add_issue(self, msg, exception=None):
        self.issues.append(Issue(stage="scicat", msg=msg, exception=exception))

    def ingest_run(self, filepath, run_start,  descriptor_doc, thumbnail=None):
        if can_info:
            info(f"Ingesting file {filepath}")
        try:
            projected_start_doc = project_start_doc(run_start, "app")
        except Exception as e:
            logger.error("Unable to project using intent of app")
            self._add_issue("error projecting start document", e)
        if can_debug:
            debug(f"projected start doc: {str(project_start_doc)}")
        # make an access grop list that includes the name of the proposal and the name of the beamline
        access_groups = []
        access_groups.append(projected_start_doc.get('proposal'))
        access_groups.append(projected_start_doc.get('beamline'))
        
        self._create_sample(projected_start_doc, access_groups)
        scientific_metadata = self._extract_scientific_metadata(descriptor_doc)
        self._create_raw_dataset(projected_start_doc, scientific_metadata, access_groups, filepath, thumbnail)

    def _create_sample(self, projected_start_doc, access_groups):
        sample = {
            "sampleId": projected_start_doc.get('sample_id'),
            "owner": projected_start_doc.get('pi_name'),
            "description": projected_start_doc.get('sample_name'),
            "createdAt": datetime.isoformat(datetime.utcnow()) + "Z",
            "sampleCharacteristics": {},
            "isPublished": False,
            "ownerGroup": projected_start_doc.get('proposal'),
            "accessGroups": access_groups,
            "createdBy": self.username,
            "updatedBy": self.username,
            "updatedAt": datetime.isoformat(datetime.utcnow()) + "Z"
        }
        sample_url = f'{self.baseurl}Samples'
        resp = self._send_to_scicat(sample_url, sample)
        if not resp.ok:  # can happen if sample id is a duplicate, but we can't tell that from the response
            err = resp.json()["error"]
            logger.error(f'could not create sample  {err["statusCode"]}: {err["message"]}')
            self._add_issue(f'could not create sample  {err["statusCode"]}: {err["message"]}')

    def _create_raw_dataset(self, projected_start_doc, scientific_metadata, access_groups, filepath, thumbnail):
        principalInvestigator = projected_start_doc.get('pi_name')
        if not principalInvestigator:
            principalInvestigator = "uknonwn"
        creationLocation = projected_start_doc.get('beamline')
        if not creationLocation:
            creationLocation = "unknown"
        # model for the raw datasets as defined in the RawDatasets
        data = { 
            "owner": projected_start_doc.get('pi_name'),
            "contactEmail": "dmcreynolds@lbl.gov",
            "createdBy": self.username,
            "updatedBy": self.username,
            "creationLocation": creationLocation,
            "updatedAt": datetime.isoformat(datetime.utcnow()) + "Z",
            "createdAt": datetime.isoformat(datetime.utcnow()) + "Z",
            "creationTime": (datetime.isoformat(datetime.fromtimestamp(
                projected_start_doc.get('collection_date'))) + "Z"),
            "datasetName": filepath.stem,
            "type": "raw",
            "instrumentId": projected_start_doc.get('instrument_name'),
            "ownerGroup": projected_start_doc.get('proposal'),
            "accessGroups": access_groups,
            "proposalId": projected_start_doc.get('proposal'),
            "dataFormat": "DX",
            "principalInvestigator": principalInvestigator,
            "sourceFolder": filepath.parent.as_posix(),
            "size": self._get_file_size(filepath),
            "scientificMetadata": scientific_metadata,
            "sampleId": projected_start_doc.get('sample_id'),
            "isPublished": False
        }
        urlAdd = "RawDatasets"
        encoded_data = json.loads(json.dumps(data, cls=NPArrayEncoder))

        if thumbnail and thumbnail.exists():
            npid = self._upload_bytes(pid=self.pid, urlAdd=urlAdd, data=encoded_data, attachFile=thumbnail)
        else:
            npid = self._upload_bytes(pid=self.pid, urlAdd=urlAdd, data=encoded_data)
        if can_info:
            info(f"npid {npid} created")

        datasetType = "RawDatasets"
        dataBlock = {
            # "id": npid,
            "size": self._get_file_size(filepath),
            "dataFileList": [
                {
                    "path": str(filepath.absolute()),
                    "size": self._get_file_size(filepath),
                    "time": self._get_file_mod_time(filepath),
                    "chk": "",  # do not do remote: getFileChecksumFromPathObj(filename)
                    "uid": str(
                        filepath.stat().st_uid
                    ),  # not implemented on windows: filename.owner(),
                    "gid": str(filepath.stat().st_gid),
                    "perm": str(filepath.stat().st_mode),
                }
            ],
            "ownerGroup": projected_start_doc.get('proposal'),
            "accessGroups": access_groups,
            "createdBy": "datasetUpload",
            "updatedBy": "datasetUpload",
            "datasetId": npid,
            "updatedAt": datetime.isoformat(datetime.utcnow()) + "Z",
            "createdAt": datetime.isoformat(datetime.utcnow()) + "Z",
        }
        url = self.baseurl + f"{datasetType}/{urllib.parse.quote_plus(npid)}/origdatablocks"
        if can_debug:
            debug(f"sending to {url}")
        resp = self._send_to_scicat(url, dataBlock)
        if not resp.ok:
            err = resp.json()["error"]
            logger.error(f'could not create sample  {err["statusCode"]}: {err["message"]}')
            self._add_issue(f'could not create sample  {err["statusCode"]}: {err["message"]}')
        if can_info:
            info(f"origdatablock sent for {npid}")
        
    @staticmethod
    def _extract_scientific_metadata(descriptor):
        retrun_dict = {k.replace(":", "/"): v for k, v in descriptor['configuration']['all']['data'].items()}
        return retrun_dict

    @staticmethod
    def _get_file_mod_time(pathobj):
        # may only work on WindowsPath objects...
        # timestamp = pathobj.lstat().st_mtime
        return str(datetime.fromtimestamp(pathobj.lstat().st_mtime))

    # def _getEntries(self, url, whereDict={}):
    #     # gets the complete response when searching for a particular entry based on a dictionary of keyword-value pairs
    #     resp = self._sendToSciCat(url, {"filter": {"where": whereDict}}, cmd="get")
    #     self.entries = resp
    #     return resp

    # def _getPid(self, url, whereDict={}, returnIfNone=0, returnField='pid'):
    #     # returns only the (first matching) pid (or proposalId in case of proposals) matching a given search request
    #     resp = self._get_entries(url, whereDict)
    #     if resp == []:
    #         # no raw dataset available
    #         pid = returnIfNone
    #     else:
    #         pid = resp[0][returnField]
    #     self.pid = pid
    #     return pid

   
    def _upload_bytes(self, pid=0, urlAdd=None, data=None, attachFile=None):
        # upload the bits to the database
        # try sending it...
        if not self.test and pid == 0:  # and not self.uploadType in ["samples"]:
            logger.info("* * * * creating new entry")
            url = self.baseurl + f"{urlAdd}/replaceOrCreate"
            resp_json = self._send_to_scicat(url, data).json()
            if "pid" in resp_json:
                npid = resp_json["pid"]
            elif "id" in resp_json:
                npid = resp_json["id"]
            elif "proposalId" in resp_json:
                npid = resp_json["proposalId"]

        elif not self.test and pid != 0:  # and not adict["uploadType"] in ["samples"]:
            logger.info("* * * * updating existing entry")
            url = self.baseurl + f"{urlAdd}/{urllib.parse.quote_plus(pid)}"
            resp = self._send_to_scicat(url, data, "patch").json()
            npid = pid

        if attachFile is not None:
            # attach an additional file as "thumbnail"
            assert isinstance(attachFile, Path), 'attachFile must be an instance of pathlib.Path'

            if attachFile.exists():
                logger.info("attaching thumbnail {} to {} \n".format(attachFile, npid))
                urlAddThumbnail = urlAdd
                if urlAdd == "DerivedDatasets":
                    urlAddThumbnail = "datasets"  # workaround for scicat inconsistency
                resp = self._addThumbnail(npid, attachFile, datasetType=urlAddThumbnail)
                if can_info:
                    info(f"uploaded thumbnail for {npid}")
        return npid

    def _addThumbnail(self, datasetId=None, filename=None, datasetType="RawDatasets"):

        def encodeImageToThumbnail(filename, imType='jpg'):
            header = "data:image/{imType};base64,".format(imType=imType)
            with open(filename, 'rb') as f:
                data = f.read()
            dataBytes = base64.b64encode(data)
            dataStr = dataBytes.decode('UTF-8')
            return header + dataStr

        dataBlock = {
            "caption": filename.stem,
            "thumbnail": encodeImageToThumbnail(filename),
            "datasetId": datasetId,
            "ownerGroup": "ingestor",
        }

        url = self.baseurl + f"{datasetType}/{urllib.parse.quote_plus(datasetId)}/attachments"
        logging.debug(url)
        resp = requests.post(
                    url,
                    params={"access_token": self.token},
                    timeout=self.timeouts,
                    stream=False,
                    json=dataBlock,
                    verify=self.sslVerify,
                )
        return resp


class NPArrayEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return json.JSONEncoder.default(self, obj)


def gen_ev_docs(scm: ScicatIngestor, filename: str, mapping_file: str):
    with open(mapping_file, 'r') as json_file:
        data = json.load(json_file)
    map = Mapping(**data)
    with h5py.File(filename, 'r') as h5_file:
        ingestor = MappedH5Generator(
            map,
            h5_file,
            'root',
            thumbs_root='/home/dylan/data/beamlines/als832/thumbs',
            data_groups=['als832'])
        descriptor = None
        start_doc = None
        for name, doc in ingestor.generate_docstream():
            if 'start' in name:
                start_doc = doc
                continue
            if 'descriptor' in name:
                descriptor = doc
                continue
            else:
                continue
        pprint(ingestor.issues)
        # descriptor.map()
        scm.ingest_run(Path(filename), start_doc, descriptor_doc=descriptor, thumbnail=ingestor.thumbnails[0])


def project_start_doc(start_doc, intent):
    found_projection = None
    projection = {}
    for projection in start_doc.get('projections'):
        configuration = projection.get('configuration')
        if configuration is None:
            continue
        if configuration.get('intent') == intent:
            if found_projection:
                raise Exception(f"Found more than one projection matching intent: {intent}")
            found_projection = projection
    if not found_projection:
        raise Exception(f"Could not find a projection matching intent: {intent}")
    projected_doc = {}
    for field, value in found_projection['projection'].items():
        if value['location'] == "start":
            projected_doc[field] = start_doc.get(value['field'])
    return projected_doc


if __name__ == "__main__":
    ch = logging.StreamHandler()
    # ch.setLevel(logging.INFO)
    # root_logger.addHandler(ch)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.setLevel(logging.DEBUG)
    can_debug = logger.isEnabledFor(logging.DEBUG)
    can_info = logger.isEnabledFor(logging.INFO)

    scm = ScicatIngestor(password="23ljlkw")
    gen_ev_docs(scm, '/home/dylan/data/beamlines/als832/20210421_091523_test3.h5', './mappings/832Mapping.json')
