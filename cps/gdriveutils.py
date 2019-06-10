#!/usr/bin/env python
# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2018 idalin, OzzieIsaacs
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <http://www.gnu.org/licenses/>.

from __future__ import division, print_function, unicode_literals
import os
import shutil

from flask import Response, stream_with_context
from sqlalchemy import create_engine
from sqlalchemy import Column, UniqueConstraint
from sqlalchemy import String, Integer
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.ext.declarative import declarative_base

try:
    from pydrive.auth import GoogleAuth
    from pydrive.drive import GoogleDrive
    from pydrive.auth import RefreshError
    from apiclient import errors
    gdrive_support = True
except ImportError:
    gdrive_support = False

from . import logger, cli, config
from .constants import BASE_DIR as _BASE_DIR


SETTINGS_YAML  = os.path.join(_BASE_DIR, 'settings.yaml')
CREDENTIALS    = os.path.join(_BASE_DIR, 'gdrive_credentials')
CLIENT_SECRETS = os.path.join(_BASE_DIR, 'client_secrets.json')

# log = logger.create()


class Singleton:
    """
    A non-thread-safe helper class to ease implementing singletons.
    This should be used as a decorator -- not a metaclass -- to the
    class that should be a singleton.

    The decorated class can define one `__init__` function that
    takes only the `self` argument. Also, the decorated class cannot be
    inherited from. Other than that, there are no restrictions that apply
    to the decorated class.

    To get the singleton instance, use the `Instance` method. Trying
    to use `__call__` will result in a `TypeError` being raised.

    """

    def __init__(self, decorated):
        self._decorated = decorated

    def Instance(self):
        """
        Returns the singleton instance. Upon its first call, it creates a
        new instance of the decorated class and calls its `__init__` method.
        On all subsequent calls, the already created instance is returned.

        """
        try:
            return self._instance
        except AttributeError:
            self._instance = self._decorated()
            return self._instance

    def __call__(self):
        raise TypeError('Singletons must be accessed through `Instance()`.')

    def __instancecheck__(self, inst):
        return isinstance(inst, self._decorated)


@Singleton
class Gauth:
    def __init__(self):
        self.auth = GoogleAuth(settings_file=SETTINGS_YAML)


@Singleton
class Gdrive:
    def __init__(self):
        self.drive = getDrive(gauth=Gauth.Instance().auth)

def is_gdrive_ready():
    return os.path.exists(SETTINGS_YAML) and os.path.exists(CREDENTIALS)


engine = create_engine('sqlite:///{0}'.format(cli.gdpath), echo=False)
Base = declarative_base()

# Open session for database connection
Session = sessionmaker()
Session.configure(bind=engine)
session = scoped_session(Session)


class GdriveId(Base):
    __tablename__ = 'gdrive_ids'

    id = Column(Integer, primary_key=True)
    gdrive_id = Column(Integer, unique=True)
    path = Column(String)
    __table_args__ = (UniqueConstraint('gdrive_id', 'path', name='_gdrive_path_uc'),)

    def __repr__(self):
        return str(self.path)


class PermissionAdded(Base):
    __tablename__ = 'permissions_added'

    id = Column(Integer, primary_key=True)
    gdrive_id = Column(Integer, unique=True)

    def __repr__(self):
        return str(self.gdrive_id)


def migrate():
    if not engine.dialect.has_table(engine.connect(), "permissions_added"):
        PermissionAdded.__table__.create(bind = engine)
    for sql in session.execute("select sql from sqlite_master where type='table'"):
        if 'CREATE TABLE gdrive_ids' in sql[0]:
            currUniqueConstraint = 'UNIQUE (gdrive_id)'
            if currUniqueConstraint in sql[0]:
                sql=sql[0].replace(currUniqueConstraint, 'UNIQUE (gdrive_id, path)')
                sql=sql.replace(GdriveId.__tablename__, GdriveId.__tablename__ + '2')
                session.execute(sql)
                session.execute("INSERT INTO gdrive_ids2 (id, gdrive_id, path) SELECT id, "
                                "gdrive_id, path FROM gdrive_ids;")
                session.commit()
                session.execute('DROP TABLE %s' % 'gdrive_ids')
                session.execute('ALTER TABLE gdrive_ids2 RENAME to gdrive_ids')
            break

if not os.path.exists(cli.gdpath):
    try:
        Base.metadata.create_all(engine)
    except Exception:
        raise
migrate()


def getDrive(drive=None, gauth=None):
    if not drive:
        if not gauth:
            gauth = GoogleAuth(settings_file=SETTINGS_YAML)
        # Try to load saved client credentials
        gauth.LoadCredentialsFile(CREDENTIALS)
        if gauth.access_token_expired:
            # Refresh them if expired
            try:
                gauth.Refresh()
            except RefreshError as e:
                logger.error("Google Drive error: %s", e)
            except Exception as e:
                logger.exception(e)
        else:
            # Initialize the saved creds
            gauth.Authorize()
        # Save the current credentials to a file
        return GoogleDrive(gauth)
    if drive.auth.access_token_expired:
        try:
            drive.auth.Refresh()
        except RefreshError as e:
            logger.error("Google Drive error: %s", e)
    return drive

def listRootFolders():
    drive = getDrive(Gdrive.Instance().drive)
    folder = "'root' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    fileList = drive.ListFile({'q': folder}).GetList()
    return fileList


def getEbooksFolder(drive):
    return getFolderInFolder('root',config.config_google_drive_folder,drive)


def getFolderInFolder(parentId, folderName, drive):
    # drive = getDrive(drive)
    query=""
    if folderName:
        query = "title = '%s' and " % folderName.replace("'", r"\'")
    folder = query + "'%s' in parents and mimeType = 'application/vnd.google-apps.folder'" \
                     " and trashed = false" % parentId
    fileList = drive.ListFile({'q': folder}).GetList()
    if fileList.__len__() == 0:
        return None
    else:
        return fileList[0]

# Search for id of root folder in gdrive database, if not found request from gdrive and store in internal database
def getEbooksFolderId(drive=None):
    storedPathName = session.query(GdriveId).filter(GdriveId.path == '/').first()
    if storedPathName:
        return storedPathName.gdrive_id
    else:
        gDriveId = GdriveId()
        try:
            gDriveId.gdrive_id = getEbooksFolder(drive)['id']
        except Exception:
            logger.error('Error gDrive, root ID not found')
        gDriveId.path = '/'
        session.merge(gDriveId)
        session.commit()
        return


def getFile(pathId, fileName, drive):
    metaDataFile = "'%s' in parents and trashed = false and title = '%s'" % (pathId, fileName.replace("'", r"\'"))
    fileList = drive.ListFile({'q': metaDataFile}).GetList()
    if fileList.__len__() == 0:
        return None
    else:
        return fileList[0]


def getFolderId(path, drive):
    # drive = getDrive(drive)
    currentFolderId = getEbooksFolderId(drive)
    sqlCheckPath = path if path[-1] == '/' else path + '/'
    storedPathName = session.query(GdriveId).filter(GdriveId.path == sqlCheckPath).first()

    if not storedPathName:
        dbChange = False
        s = path.split('/')
        for i, x in enumerate(s):
            if len(x) > 0:
                currentPath = "/".join(s[:i+1])
                if currentPath[-1] != '/':
                    currentPath = currentPath + '/'
                storedPathName = session.query(GdriveId).filter(GdriveId.path == currentPath).first()
                if storedPathName:
                    currentFolderId = storedPathName.gdrive_id
                else:
                    currentFolder = getFolderInFolder(currentFolderId, x, drive)
                    if currentFolder:
                        gDriveId = GdriveId()
                        gDriveId.gdrive_id = currentFolder['id']
                        gDriveId.path = currentPath
                        session.merge(gDriveId)
                        dbChange = True
                        currentFolderId = currentFolder['id']
                    else:
                        currentFolderId = None
                        break
        if dbChange:
            session.commit()
    else:
        currentFolderId = storedPathName.gdrive_id
    return currentFolderId


def getFileFromEbooksFolder(path, fileName):
    drive = getDrive(Gdrive.Instance().drive)
    if path:
        # sqlCheckPath=path if path[-1] =='/' else path + '/'
        folderId = getFolderId(path, drive)
    else:
        folderId = getEbooksFolderId(drive)
    if folderId:
        return getFile(folderId, fileName, drive)
    else:
        return None


def moveGdriveFileRemote(origin_file_id, new_title):
    origin_file_id['title']= new_title
    origin_file_id.Upload()


# Download metadata.db from gdrive
def downloadFile(path, filename, output):
    f = getFileFromEbooksFolder(path, filename)
    f.GetContentFile(output)


def moveGdriveFolderRemote(origin_file, target_folder):
    drive = getDrive(Gdrive.Instance().drive)
    previous_parents = ",".join([parent["id"] for parent in origin_file.get('parents')])
    children = drive.auth.service.children().list(folderId=previous_parents).execute()
    gFileTargetDir = getFileFromEbooksFolder(None, target_folder)
    if not gFileTargetDir:
        # Folder is not existing, create, and move folder
        gFileTargetDir = drive.CreateFile(
            {'title': target_folder, 'parents': [{"kind": "drive#fileLink", 'id': getEbooksFolderId()}],
             "mimeType": "application/vnd.google-apps.folder"})
        gFileTargetDir.Upload()
    # Move the file to the new folder
    drive.auth.service.files().update(fileId=origin_file['id'],
                                      addParents=gFileTargetDir['id'],
                                      removeParents=previous_parents,
                                      fields='id, parents').execute()
    # if previous_parents has no childs anymore, delete original fileparent
    if len(children['items']) == 1:
        deleteDatabaseEntry(previous_parents)
        drive.auth.service.files().delete(fileId=previous_parents).execute()


def copyToDrive(drive, uploadFile, createRoot, replaceFiles,
        ignoreFiles=None,
        parent=None, prevDir=''):
    ignoreFiles = ignoreFiles or []
    drive = getDrive(drive)
    isInitial = not bool(parent)
    if not parent:
        parent = getEbooksFolder(drive)
    if os.path.isdir(os.path.join(prevDir,uploadFile)):
        existingFolder = drive.ListFile({'q': "title = '%s' and '%s' in parents and trashed = false" %
                                              (os.path.basename(uploadFile).replace("'", r"\'"), parent['id'])}).GetList()
        if len(existingFolder) == 0 and (not isInitial or createRoot):
            parent = drive.CreateFile({'title': os.path.basename(uploadFile),
                                       'parents': [{"kind": "drive#fileLink", 'id': parent['id']}],
                "mimeType": "application/vnd.google-apps.folder"})
            parent.Upload()
        else:
            if (not isInitial or createRoot) and len(existingFolder) > 0:
                parent = existingFolder[0]
        for f in os.listdir(os.path.join(prevDir, uploadFile)):
            if f not in ignoreFiles:
                copyToDrive(drive, f, True, replaceFiles, ignoreFiles, parent, os.path.join(prevDir, uploadFile))
    else:
        if os.path.basename(uploadFile) not in ignoreFiles:
            existingFiles = drive.ListFile({'q': "title = '%s' and '%s' in parents and trashed = false" %
                                                 (os.path.basename(uploadFile).replace("'", r"\'"), parent['id'])}).GetList()
            if len(existingFiles) > 0:
                driveFile = existingFiles[0]
            else:
                driveFile = drive.CreateFile({'title': os.path.basename(uploadFile).replace("'", r"\'"),
                                              'parents': [{"kind":"drive#fileLink", 'id': parent['id']}], })
            driveFile.SetContentFile(os.path.join(prevDir, uploadFile))
            driveFile.Upload()


def uploadFileToEbooksFolder(destFile, f):
    drive = getDrive(Gdrive.Instance().drive)
    parent = getEbooksFolder(drive)
    splitDir = destFile.split('/')
    for i, x in enumerate(splitDir):
        if i == len(splitDir)-1:
            existingFiles = drive.ListFile({'q': "title = '%s' and '%s' in parents and trashed = false" %
                                                 (x.replace("'", r"\'"), parent['id'])}).GetList()
            if len(existingFiles) > 0:
                driveFile = existingFiles[0]
            else:
                driveFile = drive.CreateFile({'title': x, 'parents': [{"kind": "drive#fileLink", 'id': parent['id']}],})
            driveFile.SetContentFile(f)
            driveFile.Upload()
        else:
            existingFolder = drive.ListFile({'q': "title = '%s' and '%s' in parents and trashed = false" %
                                                  (x.replace("'", r"\'"), parent['id'])}).GetList()
            if len(existingFolder) == 0:
                parent = drive.CreateFile({'title': x, 'parents': [{"kind": "drive#fileLink", 'id': parent['id']}],
                    "mimeType": "application/vnd.google-apps.folder"})
                parent.Upload()
            else:
                parent = existingFolder[0]


def watchChange(drive, channel_id, channel_type, channel_address,
              channel_token=None, expiration=None):
    # Watch for all changes to a user's Drive.
    # Args:
    # service: Drive API service instance.
    # channel_id: Unique string that identifies this channel.
    # channel_type: Type of delivery mechanism used for this channel.
    # channel_address: Address where notifications are delivered.
    # channel_token: An arbitrary string delivered to the target address with
    #               each notification delivered over this channel. Optional.
    # channel_address: Address where notifications are delivered. Optional.
    # Returns:
    # The created channel if successful
    # Raises:
    # apiclient.errors.HttpError: if http request to create channel fails.
    body = {
        'id': channel_id,
        'type': channel_type,
        'address': channel_address
    }
    if channel_token:
        body['token'] = channel_token
    if expiration:
        body['expiration'] = expiration
    return drive.auth.service.changes().watch(body=body).execute()


def watchFile(drive, file_id, channel_id, channel_type, channel_address,
              channel_token=None, expiration=None):
    """Watch for any changes to a specific file.
    Args:
    service: Drive API service instance.
    file_id: ID of the file to watch.
    channel_id: Unique string that identifies this channel.
    channel_type: Type of delivery mechanism used for this channel.
    channel_address: Address where notifications are delivered.
    channel_token: An arbitrary string delivered to the target address with
                   each notification delivered over this channel. Optional.
    channel_address: Address where notifications are delivered. Optional.
    Returns:
    The created channel if successful
    Raises:
    apiclient.errors.HttpError: if http request to create channel fails.
    """
    body = {
        'id': channel_id,
        'type': channel_type,
        'address': channel_address
    }
    if channel_token:
        body['token'] = channel_token
    if expiration:
        body['expiration'] = expiration
    return drive.auth.service.files().watch(fileId=file_id, body=body).execute()


def stopChannel(drive, channel_id, resource_id):
    """Stop watching to a specific channel.
    Args:
    service: Drive API service instance.
    channel_id: ID of the channel to stop.
    resource_id: Resource ID of the channel to stop.
    Raises:
    apiclient.errors.HttpError: if http request to create channel fails.
    """
    body = {
        'id': channel_id,
        'resourceId': resource_id
    }
    return drive.auth.service.channels().stop(body=body).execute()


def getChangeById (drive, change_id):
    # Print a single Change resource information.
    #
    # Args:
    # service: Drive API service instance.
    # change_id: ID of the Change resource to retrieve.
    try:
        change = drive.auth.service.changes().get(changeId=change_id).execute()
        return change
    except (errors.HttpError) as error:
        logger.error(error)
        return None
    except Exception as e:
        logger.error(e)
        return None


# Deletes the local hashes database to force search for new folder names
def deleteDatabaseOnChange():
    session.query(GdriveId).delete()
    session.commit()

def updateGdriveCalibreFromLocal():
    copyToDrive(Gdrive.Instance().drive, config.config_calibre_dir, False, True)
    for x in os.listdir(config.config_calibre_dir):
        if os.path.isdir(os.path.join(config.config_calibre_dir, x)):
            shutil.rmtree(os.path.join(config.config_calibre_dir, x))

# update gdrive.db on edit of books title
def updateDatabaseOnEdit(ID,newPath):
    sqlCheckPath = newPath if newPath[-1] == '/' else newPath + u'/'
    storedPathName = session.query(GdriveId).filter(GdriveId.gdrive_id == ID).first()
    if storedPathName:
        storedPathName.path = sqlCheckPath
        session.commit()


# Deletes the hashes in database of deleted book
def deleteDatabaseEntry(ID):
    session.query(GdriveId).filter(GdriveId.gdrive_id == ID).delete()
    session.commit()


# Gets cover file from gdrive
def get_cover_via_gdrive(cover_path):
    df = getFileFromEbooksFolder(cover_path, 'cover.jpg')
    if df:
        if not session.query(PermissionAdded).filter(PermissionAdded.gdrive_id == df['id']).first():
            df.GetPermissions()
            df.InsertPermission({
                            'type': 'anyone',
                            'value': 'anyone',
                            'role': 'reader',
                            'withLink': True})
            permissionAdded = PermissionAdded()
            permissionAdded.gdrive_id = df['id']
            session.add(permissionAdded)
            session.commit()
        return df.metadata.get('webContentLink')
    else:
        return None

# Creates chunks for downloading big files
def partial(total_byte_len, part_size_limit):
    s = []
    for p in range(0, total_byte_len, part_size_limit):
        last = min(total_byte_len - 1, p + part_size_limit - 1)
        s.append([p, last])
    return s

# downloads files in chunks from gdrive
def do_gdrive_download(df, headers):
    total_size = int(df.metadata.get('fileSize'))
    download_url = df.metadata.get('downloadUrl')
    s = partial(total_size, 1024 * 1024)  # I'm downloading BIG files, so 100M chunk size is fine for me

    def stream():
        for byte in s:
            headers = {"Range": 'bytes=%s-%s' % (byte[0], byte[1])}
            resp, content = df.auth.Get_Http_Object().request(download_url, headers=headers)
            if resp.status == 206:
                yield content
            else:
                logger.warning('An error occurred: %s', resp)
                return
    return Response(stream_with_context(stream()), headers=headers)
