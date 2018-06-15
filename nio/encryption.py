# -*- coding: utf-8 -*-

# Copyright © 2018 Damir Jelić <poljar@termina.org.uk>
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

from __future__ import unicode_literals

import json
import os
import sqlite3
import pprint
# pylint: disable=redefined-builtin
from builtins import str, bytes, super
from collections import defaultdict, deque
from functools import wraps
from typing import *

from jsonschema import ValidationError, SchemaError
from logbook import Logger
from olm import (Account, InboundGroupSession, InboundSession, OlmAccountError,
                 OlmGroupSessionError, OlmMessage, OlmPreKeyMessage,
                 OlmSessionError, OutboundGroupSession, OutboundSession,
                 Session)

from .schemas import Schemas, validate_json
from .log import logger_group

logger = Logger('nio.encryption')
logger_group.add_logger(logger)

try:
    from json.decoder import JSONDecodeError
except ImportError:  # pragma: no cover
    JSONDecodeError = ValueError  # type: ignore


try:
    FileNotFoundError  # type: ignore
except NameError:  # pragma: no cover
    FileNotFoundError = IOError


class OlmTrustError(Exception):
    pass


class EncryptionError(Exception):
    pass


class VerificationError(Exception):
    pass


OlmEvent = NamedTuple("OlmEvent", [
    ("sender", str),
    ("sender_key", str),
    ("payload_dict", Dict[Any, Any])
    ])


class Key(object):
    def __init__(self, user_id, device_id, key):
        # type: (str, str, str) -> None
        self.user_id = user_id
        self.device_id = device_id
        self.key = key

    @classmethod
    def from_line(cls, line):
        # type: (str) -> Optional[Key]
        fields = line.split(' ')

        if len(fields) < 4:
            return None

        user_id, device_id, key_type, key = fields[:4]

        if key_type == "matrix-ed25519":
            return Ed25519Key(user_id, device_id, key)
        else:
            return None

    def to_line(self):
        # type: () -> str
        key_type = ""

        if isinstance(self, Ed25519Key):
            key_type = "matrix-ed25519"
        else:
            raise NotImplementedError("Invalid key type {}".format(
                type(self.key)))

        line = "{} {} {} {}\n".format(
            self.user_id,
            self.device_id,
            key_type,
            str(self.key)
        )
        return line

    @classmethod
    def from_olmdevice(cls, device):
        # type: (OlmDevice) -> Optional[Ed25519Key]
        user_id = device.user_id
        device_id = device.id

        for key_type, key in device.keys.items():
            if key_type == "ed25519":
                return Ed25519Key(user_id, device_id, key)

        return None


class Ed25519Key(Key):
    def __eq__(self, value):
        # type: (object) -> bool
        if not isinstance(value, Ed25519Key):
            return NotImplemented

        if (self.user_id == value.user_id
                and self.device_id == value.device_id
                and self.key == value.key):
            return True

        return False


class DeviceStore(object):
    def __init__(self, filename):
        # type: (str) -> None
        self._entries = []  # type: List[OlmDevice]
        self._fingerprint_store = KeyStore(filename)  \
            # type: KeyStore

    def __iter__(self):
        # type: () -> Iterator[OlmDevice]
        for entry in self._entries:
            yield entry

    def add(self, device):
        # type: (OlmDevice) -> bool
        if device in self._entries:
            return False

        self._fingerprint_store.add(Key.from_olmdevice(device))

        self._entries.append(device)
        return True

    def user_devices(self, user_id):
        # type: (str) -> List[OlmDevice]
        devices = []

        for entry in self._entries:
            if user_id == entry.user_id:
                devices.append(entry)

        return devices

    @staticmethod
    def _verify_key(device, key):
        # type: (OlmDevice, Key) -> bool
        if isinstance(key, Ed25519Key):
            return device.keys["ed25519"] == key.key
        else:
            raise NotImplementedError("Key verification for key type {} not "
                                      "implemented".format(type(key)))

    def verify_key(self, key):
        # type: (Key) -> bool
        for entry in self._entries:
            if (key.user_id == entry.user_id
                    and key.device_id == entry.id):
                return self._verify_key(entry, key)

        raise KeyError("No key found for user {} and device {}".format(
            key.user_id,
            key.device_id
        ))


class KeyStore(object):
    def __init__(self, filename):
        # type: (str) -> None
        self._entries = []  # type: List[Key]
        self._filename = filename  # type: str

        self._load(filename)

    def __iter__(self):
        # type: () -> Iterator[Key]
        for entry in self._entries:
            yield entry

    def __repr__(self):
        # type: () -> str
        return "FingerprintStore object, store file: {}".format(self._filename)

    def _load(self, filename):
        # type: (str) -> None
        try:
            with open(filename, "r") as f:
                for line in f:
                    line = line.strip()

                    if not line or line.startswith("#"):
                        continue

                    entry = Key.from_line(line)

                    if not entry:
                        continue

                    self._entries.append(entry)
        except FileNotFoundError:
            pass

    def get_key(self, user_id, device_id):
        # type: (str, str) -> Optional[Key]
        for entry in self._entries:
            if user_id == entry.user_id and device_id == entry.device_id:
                return entry

        return None

    def _save_store(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            self = args[0]
            ret = f(*args, **kwargs)
            self._save()
            return ret

        return decorated

    def _save(self):
        # type: () -> None
        with open(self._filename, "w") as f:
            for entry in self._entries:
                line = entry.to_line()
                f.write(line)

    @_save_store
    def add(self, key):
        # type: (Key) -> bool
        existing_key = self.get_key(key.user_id, key.device_id)

        if existing_key:
            if (existing_key.user_id == key.user_id
                    and existing_key.device_id == key.device_id
                    and type(existing_key) is type(key)):
                if existing_key.key != key.key:
                    message = ("Error: adding existing device to trust store "
                               "with mismatching fingerprint {} {}".format(
                                   key.key,
                                   existing_key.key
                               ))
                    logger.error(message)
                    raise OlmTrustError(message)

        self._entries.append(key)
        self._save()
        return True

    @_save_store
    def remove(self, key):
        # type: (Key) -> bool
        if key in self._entries:
            self._entries.remove(key)
            self._save()
            return True

        return False

    def check(self, key):
        # type: (Key) -> bool
        return key in self._entries


class OlmDevice(object):
    def __init__(self, user_id, device_id, key_dict):
        # type: (str, str, Dict[str, str]) -> None
        self.user_id = user_id
        self.id = device_id
        self.keys = key_dict

    def __str__(self):
        # type: () -> str
        line = "{} {} {}".format(
            self.user_id,
            self.id,
            pprint.pformat(self.keys)
        )
        return line

    def __eq__(self, value):
        # type: (object) -> bool
        if not isinstance(value, OlmDevice):
            return NotImplemented

        try:
            # We only care for the fingerprint key.
            if (self.user_id == value.user_id
                    and self.id == value.id
                    and self.keys["ed25519"] == value.keys["ed25519"]):
                return True
        except KeyError:
            pass

        return False


class OneTimeKey(object):
    def __init__(self, user_id, device_id, key, key_type):
        # type: (str, str, str, str) -> None
        self.user_id = user_id
        self.device_id = device_id
        self.key = key
        self.key_type = key_type


class OlmSession(object):
    def __init__(self, user_id, device_id, identity_key, session):
        # type: (str, str, str, Session) -> None
        self.user_id = user_id
        self.device_id = device_id
        self.identity_key = identity_key
        self.session = session

    @property
    def id(self):
        # type: () -> str
        return "{}:{}:{}".format(self.user_id, self.device_id, self.session.id)

    def __eq__(self, value):
        # type: (object) -> bool
        if not isinstance(value, OlmSession):
            return NotImplemented

        if (self.user_id == value.user_id
                and self.device_id == value.device_id
                and self.identity_key == value.identity_key
                and self.session.id == value.session.id):
            return True

        return False

    def encrypt(self, plaintext):
        # type: (str) -> Union[OlmPreKeyMessage, OlmMessage]
        return self.session.encrypt(plaintext)

    def decrypt(self, message):
        # type: (Union[OlmMessage, OlmPreKeyMessage]) -> str
        return self.session.decrypt(message)

    def matches(self, message):
        # type: (Union[OlmMessage, OlmPreKeyMessage]) -> bool
        return self.session.matches(message)


class SessionStore(object):
    def __init__(self):
        # type: () -> None
        self._entries = defaultdict(list)  \
            # type: DefaultDict[str, List[Session]]

    def add(self, session):
        # type: (OlmSession) -> bool
        if session in self._entries[session.identity_key]:
            return False

        self._entries[session.identity_key].append(session)
        self._entries[session.identity_key].sort(
            key=lambda x: x.session.id
        )
        return True

    def __iter__(self):
        # type: () -> Iterator[OlmSession]
        for session_list in self._entries.values():
            for session in session_list:
                yield session

    def check(self, session):
        # type: (OlmSession) -> bool
        if session in self._entries[session.identity_key]:
            return True
        return False

    def remove(self, session):
        # type: (OlmSession) -> bool
        if session in self._entries[session.identity_key]:
            self._entries[session.identity_key].remove(session)
            return True

        return False

    def get(self, identity_key):
        # type: (str) -> Optional[OlmSession]
        if self._entries[identity_key]:
            return self._entries[identity_key][0]

        return None

    def __getitem__(self, identity_key):
        # type: (str) -> List[OlmSession]
        return self._entries[identity_key]


class InGroupSession(InboundGroupSession):
    def __new__(
        cls,
        sender_key,     # type: str
        sender_fp_key,  # type: str
        room_id,        # type: str
        session_id,     # type: str
        session_key     # type: str
    ):
        return super().__new__(cls, session_key)

    def __init__(
        self,
        sender_key,     # type: str
        sender_fp_key,  # type: str
        room_id,        # type: str
        session_id,     # type: str
        session_key     # type: str
    ):
        # type: (...) -> None
        self.sender_key = sender_key
        self.sender_fp_key = sender_fp_key
        self.room_id = room_id
        super().__init__(session_key)

        if self.id != session_id:
            raise OlmSessionError("Session id misssmatch while importing "
                                  "megolm session key")


class Olm(object):
    def __init__(
        self,
        user_id,                     # type: str
        device_id,                   # type: str
        session_path,                # type: str
    ):
        # type: (...) -> None
        self.user_id = user_id
        self.device_id = device_id
        self.session_path = session_path

        self.olm_queue = deque()  # type: Deque[OlmEvent]

        # List of group session ids that we shared with people
        self.shared_sessions = []  # type: List[str]

        # TODO the folowing dicts should probably be turned into classes with
        # nice interfaces for their operations

        # Store containing devices of users that are members of encrypted rooms
        # the fingerprint keys get stored in a file so that we catch fingeprint
        # key swaps.
        device_store_file = "{}_{}.known_devices".format(user_id, device_id)
        self.devices = DeviceStore(os.path.join(
            session_path,
            device_store_file
        ))  # type: DeviceStore

        self.session_store = SessionStore()  # type: SessionStore

        # Dict of inbound Megolm sessions
        # Dict[room_id, Dict[session_id, session]]
        self.inbound_group_sessions = defaultdict(dict) \
            # type: DefaultDict[str, Dict[str, InboundGroupSession]]

        # Dict of outbound Megolm sessions Dict[room_id]
        self.outbound_group_sessions = {} \
            # type: Dict[str, OutboundGroupSession]

        loaded = self.load()

        if not loaded:
            self.account = Account()
            self.save_account(True)

        # TODO we need a db for untrusted device as well as for seen devices.
        trust_file_path = "{}_{}.trusted_devices".format(user_id, device_id)
        self.trust_db = KeyStore(os.path.join(
            session_path,
            trust_file_path
        ))

    def _create_inbound_session(
        self,
        sender,      # type: str
        sender_key,  # type: str
        message      # type: Union[OlmPreKeyMessage, OlmMessage]
    ):
        # type: (...) -> InboundSession
        logger.info("Creating Inbound session for {}".format(sender))
        # Let's create a new inbound session.
        session = InboundSession(self.account, message, sender_key)
        logger.info("Created Inbound session for {}".format(sender))
        # Remove the one time keys the session used so it can't be reused
        # anymore.
        self.account.remove_one_time_keys(session)
        # Save the account now that we removed the one time key.
        self.save_account()

        return session

    def verify_device(self, key):
        # type: (Key) -> bool
        if key in self.trust_db:
            return False

        self.trust_db.add(key)
        return True

    def device_trusted(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        if key:
            return key in self.trust_db

        return False

    def unverify_device(self, key):
        # type: (Key) -> None
        self.trust_db.remove(key)

    def create_session(self, user_id, device_id, one_time_key):
        # type: (str, str, str) -> None
        # TODO the one time key needs to be verified before calling this

        id_key = None

        # Let's create a new outbound session
        logger.info("Creating Outbound for {} and device {}".format(
            user_id, device_id))

        # We need to find the device key for the wanted user and his device.
        for device in self.devices:
            if device.user_id != user_id or device.id != device_id:
                continue

            # Found a device let's get the curve25519 key
            id_key = device.keys["curve25519"]
            break

        if not id_key:
            message = "Identity key for device {} not found".format(device_id)
            logger.error(message)
            raise EncryptionError(message)

        logger.info("Found identity key for device {}".format(device_id))
        # Create the session
        # TODO this can fail
        s = OutboundSession(self.account, id_key, one_time_key)
        # Save the account, add the session to the store and save it to the
        # database.
        self.save_account()
        session = OlmSession(user_id, device_id, id_key, s)

        self.session_store.add(session)
        self.save_session(session, new=True)
        logger.info("Created OutboundSession for device {}".format(device_id))

    def create_group_session(
        self,
        sender_key,
        sender_fp_key,
        room_id,
        session_id,
        session_key
    ):
        # type: (str, str, str, str, str) -> None
        logger.info("Creating inbound group session for {} from {}".format(
            room_id,
            sender_key
        ))

        try:
            session = InGroupSession(
                sender_key,
                sender_fp_key,
                room_id,
                session_id,
                session_key
            )
        except OlmSessionError as e:
            logger.warn(e)
            return

        self.inbound_group_sessions[room_id][session_id] = session
        # self.save_inbound_group_session(room_id, session)

    def create_outbound_group_session(self, room_id):
        # type: (str) -> None
        logger.info("Creating outbound group session for {}".format(room_id))
        session = OutboundGroupSession()
        self.outbound_group_sessions[room_id] = session
        self.create_group_session(room_id, session.id, session.session_key)
        logger.info("Created outbound group session for {}".format(room_id))

    def get_missing_sessions(self, users):
        # type: (List[str]) -> Dict[str, Dict[str, str]]
        missing = {}

        for user_id in users:
            devices = []

            for device in self.devices.user_devices(user_id):
                # we don't need a session for our own device, skip it
                if device.id == self.device_id:
                    continue

                if not self.session_store.get(device.keys["curve25519"]):
                    logger.warn("Missing session for device {}".format(
                        device.id))
                    devices.append(device.id)

            if devices:
                missing[user_id] = {device: "signed_curve25519" for
                                    device in devices}

        return missing

    def _try_decrypt(
        self,
        sender,      # type: str
        sender_key,  # type: str
        message      # type: Union[OlmPreKeyMessage, OlmMessage]
    ):
        # type: (...) -> Optional[str]
        plaintext = None

        # Let's try to decrypt with each known session for the sender.
        # for a specific device?
        for session in self.session_store[sender_key]:
            matches = False
            try:
                if isinstance(message, OlmPreKeyMessage):
                    # It's a prekey message, check if the session matches
                    # if it doesn't no need to try to decrypt.
                    matches = session.matches(message)
                    if not matches:
                        continue

                logger.info("Trying to decrypt olm message using existing "
                            "session for {} and device {}".format(
                                sender,
                                session.device_id
                            ))

                plaintext = session.decrypt(message)
                # TODO do we need to save the session in the database here?

                logger.info("Succesfully decrypted olm message "
                            "using existing session")
                return plaintext

            except OlmSessionError as e:
                # Decryption failed using a matching session, we don't want
                # to create a new session using this prekey message so
                # raise an exception and log the error.
                if matches:
                    logger.error("Found matching session yet decryption "
                                 "failed for sender {} and "
                                 "device {}".format(
                                     sender,
                                     session.device_id
                                 ))
                    raise EncryptionError("Decryption failed for matching "
                                          "session")

                # Decryption failed, we'll try another session in the next
                # iteration.
                logger.info("Error decrypting olm message from {} "
                            "and device {}: {}".format(
                                sender,
                                session.device_id,
                                str(e)
                            ))
                pass

        return None

    def _verify_olm_payload(self, sender, payload):
        # type: (str, Dict[Any, Any]) -> bool
        # Verify that the sender in the payload matches the sender of the event
        if sender != payload["sender"]:
            raise VerificationError("Missmatched sender in Olm payload")

        # Verify that we're the recipient of the payload.
        if self.user_id != payload["recipient"]:
            raise VerificationError("Missmatched recipient in Olm "
                                    "payload")

        # Verify that the recipient fingerprint key matches our own
        if (self.account.identity_keys["ed25519"] !=
                payload["recipient_keys"]["ed25519"]):
            raise VerificationError("Missmatched recipient key in "
                                    "Olm payload")

        sender_device = payload["sender_device"]
        sender_fp_key = Ed25519Key(
            sender,
            sender_device,
            payload["keys"]["ed25519"])

        # Check that the ed25519 sender key matches to the one we previously
        # downloaded using a key query, if we didn't query the keys yet raise a
        # trust error so we can put the payload in a queue and process it later
        # on
        try:
            if not self.devices.verify_key(sender_fp_key):
                raise VerificationError("Missmatched sender key in Olm payload"
                                        " for {} and device {}".format(
                                            sender,
                                            sender_device
                                        ))
        except KeyError:
            raise OlmTrustError("Fingerprint key for {} on device {} "
                                "not found".format(sender, sender_device))

        return True

    def _handle_olm_event(self, sender, sender_key, payload):
        # type: (str, str, Dict[Any, Any]) -> None
        logger.info("Recieved Olm event of type: {}".format(payload["type"]))

        if payload["type"] != "m.room_key":
            logger.warn("Received unsuported Olm event of type {}".format(
                payload["type"]))
            return

        try:
            validate_json(payload, Schemas.room_key_event)
        except (ValidationError, SchemaError) as e:
            logger.error("Error m.room_key event event from {}"
                         ": {}".format(sender, str(e.message)))
            return None

        content = payload["content"]

        if content["algorithm"] != "m.megolm.v1.aes-sha2":
            logger.error("Error: unsuported room key of type {}".format(
                payload["algorithm"]))
            return

        room_id = content["room_id"]

        logger.info("Recieved new group session key for room {} "
                    "from {}".format(room_id, sender))

        self.create_group_session(
                sender_key,
                payload["keys"]["ed25519"],
                content["room_id"],
                content["session_id"],
                content["session_key"]
        )

        return

    def decrypt(
        self,
        sender,      # type: str
        sender_key,  # type: str
        message      # type: Union[OlmPreKeyMessage, OlmMessage]
    ):
        # type: (...) -> None

        s = None
        try:
            # First try to decrypt using an existing session.
            plaintext = self._try_decrypt(sender, sender_key, message)
        except EncryptionError:
            # We found a matching session for a prekey message but decryption
            # failed, don't try to decrypt any further.
            return

        # Decryption failed with every known session or no known sessions,
        # let's try to create a new session.
        if not plaintext:
            # New sessions can only be created if it's a prekey message, we
            # can't decrypt the message if it isn't one at this point in time
            # anymore, so return early
            if not isinstance(message, OlmPreKeyMessage):
                return

            try:
                # Let's create a new session.
                s = self._create_inbound_session(sender, sender_key, message)
                # Now let's decrypt the message using the new session.
                plaintext = s.decrypt(message)
            except OlmSessionError as e:
                logger.error("Failed to create new session from prekey"
                             "message: {}".format(str(e)))
                return

        # Mypy complains that the plaintext can still be empty here,
        # realistically this can't happen but let's make mypy happy
        if not plaintext:
            logger.error("Failed to decrypt Olm message: unknown error")
            return

        # The plaintext should be valid json, let's parse it and verify it.
        try:
            parsed_payload = json.loads(plaintext, encoding='utf-8')
        except JSONDecodeError as e:
            # Failed parsing the payload, return early.
            logger.error("Failed to parse Olm message payload: {}".format(
                str(e)
            ))
            return

        # Validate the payload, check that it contains all required keys as
        # well that the types of the values are the one we expect.
        # Note: The keys of the content object aren't checked here, the caller
        # should check the content depending on the type of the event
        try:
            validate_json(parsed_payload, Schemas.olm_event)
        except (ValidationError, SchemaError) as e:
            # Something is wrong with the payload log an error and return
            # early.
            logger.error("Error validating decrypted Olm event from {}"
                         ": {}".format(sender, str(e.message)))
            return

        sender_device = parsed_payload["sender_device"]

        # Verify that the payload properties contain correct values:
        # sender/recipient/keys/recipient_keys and check if the sender device
        # is alread verified by us
        try:
            self._verify_olm_payload(sender, parsed_payload)

        except VerificationError as e:
            # We found a missmatched property don't process the event any
            # further
            logger.error(e)
            return

        except OlmTrustError as e:
            # We couldn't verify the sender fingerprint key, put the event into
            # the queue for later processing
            logger.warn(e)
            olm_event = OlmEvent(sender, sender_key, parsed_payload)
            self.olm_queue.append(olm_event)

        else:
            # Verification succeded, handle the event
            self._handle_olm_event(sender, sender_key, parsed_payload)

        finally:
            if s:
                # We created a new session, find out the device id for it and
                # store it in the session store as well as in the database.
                session = OlmSession(sender, sender_device, sender_key, s)
                self.session_store.add(session)
                self.save_session(session, new=True)

    def group_encrypt(
        self,
        room_id,         # type: str
        plaintext_dict,  # type: Dict[str, str]
        users            # type: List[str]
    ):
        # type: (...) -> Tuple[Dict[str, str], Optional[Dict[Any, Any]]]
        plaintext_dict["room_id"] = room_id
        to_device_dict = None  # type: Optional[Dict[str, Any]]

        if room_id not in self.outbound_group_sessions:
            self.create_outbound_group_session(room_id)

        if (self.outbound_group_sessions[room_id].id
                not in self.shared_sessions):
            to_device_dict = self.share_group_session(
                room_id,
                users
            )
            self.shared_sessions.append(
                self.outbound_group_sessions[room_id].id
            )

        session = self.outbound_group_sessions[room_id]

        ciphertext = session.encrypt(Olm._to_json(plaintext_dict))

        payload_dict = {
            "algorithm": "m.megolm.v1.aes-sha2",
            "sender_key": self.account.identity_keys()["curve25519"],
            "ciphertext": ciphertext,
            "session_id": session.id,
            "device_id": self.device_id
        }

        return payload_dict, to_device_dict

    def group_decrypt(self, room_id, session_id, ciphertext):
        # type: (str, str, str) -> Optional[str]
        if session_id not in self.inbound_group_sessions[room_id]:
            return None

        session = self.inbound_group_sessions[room_id][session_id]
        try:
            plaintext = session.decrypt(ciphertext)
        except OlmGroupSessionError:
            return None

        return plaintext

    def share_group_session(self, room_id, users):
        # type: (str, List[str]) -> Dict[str, Any]
        group_session = self.outbound_group_sessions[room_id]

        key_content = {
            "algorithm": "m.megolm.v1.aes-sha2",
            "room_id": room_id,
            "session_id": group_session.id,
            "session_key": group_session.session_key,
            "chain_index": group_session.message_index
        }

        payload_dict = {
            "type": "m.room_key",
            "content": key_content,
            "sender": self.user_id,
            "sender_device": self.device_id,
            "keys": {
                "ed25519": self.account.identity_keys()["ed25519"]
            }
        }

        to_device_dict = {
            "messages": {}
        }  # type: Dict[str, Any]

        for user_id in users:
            for device in self.devices.user_devices(user_id):
                # No need to share the session with our own device
                if device.id == self.device_id:
                    continue

                session = self.session_store.get(device.keys["curve25519"])

                if not session:
                    continue

                if self.device_trusted(device):
                    raise OlmTrustError

                device_payload_dict = payload_dict.copy()
                device_payload_dict["recipient"] = user_id
                device_payload_dict["recipient_keys"] = {
                    "ed25519": device.keys["ed25519"]
                }

                olm_message = session.encrypt(
                    Olm._to_json(device_payload_dict)
                )

                olm_dict = {
                    "algorithm": "m.olm.v1.curve25519-aes-sha2",
                    "sender_key": self.account.identity_keys()["curve25519"],
                    "ciphertext": {
                        device.keys["curve25519"]: {
                            "type": (0 if isinstance(
                                olm_message,
                                OlmPreKeyMessage
                            ) else 1),
                            "body": olm_message.ciphertext
                        }
                    }
                }

                if user_id not in to_device_dict["messages"]:
                    to_device_dict["messages"][user_id] = {}

                to_device_dict["messages"][user_id][device.id] = olm_dict

        return to_device_dict

    def load(self):
        # type: () -> bool

        db_file = "{}_{}.db".format(self.user_id, self.device_id)
        db_path = os.path.join(self.session_path, db_file)

        self.database = sqlite3.connect(db_path)
        new = Olm._check_db_tables(self.database)

        if new:
            return False

        cursor = self.database.cursor()

        cursor.execute(
            "select pickle from olmaccount where user = ?",
            (self.user_id,)
        )
        row = cursor.fetchone()
        account_pickle = row[0]

        cursor.execute("select user, device_id, identity_key, pickle "
                       "from olmsessions")
        db_sessions = cursor.fetchall()

        cursor.execute("select room_id, pickle from inbound_group_sessions")
        db_inbound_group_sessions = cursor.fetchall()

        cursor.close()

        try:
            try:
                account_pickle = bytes(account_pickle, "utf-8")
            except TypeError:
                pass

            self.account = Account.from_pickle(account_pickle)

            for db_session in db_sessions:
                session_pickle = db_session[3]
                try:
                    session_pickle = bytes(session_pickle, "utf-8")
                except TypeError:
                    pass

                s = Session.from_pickle(session_pickle)
                session = OlmSession(
                    db_session[0],
                    db_session[1],
                    db_session[2],
                    s
                )
                self.session_store.add(session)

            for db_session in db_inbound_group_sessions:
                session_pickle = db_session[1]
                try:
                    session_pickle = bytes(session_pickle, "utf-8")
                except TypeError:
                    pass

                s = InboundGroupSession.from_pickle(session_pickle)
                self.inbound_group_sessions[db_session[0]][s.id] = s

        except (OlmAccountError, OlmSessionError) as error:
            raise EncryptionError(error)

        return True

    def save(self):
        # type: () -> None
        self.save_account()

        for session in self.session_store:
            self.save_session(session)

    def save_session(self, session, new=False):
        # type: (OlmSession, bool) -> None
        cursor = self.database.cursor()
        if new:
            cursor.execute("insert into olmsessions values(?,?,?,?,?)", (
                session.user_id,
                session.device_id,
                session.identity_key,
                session.session.id,
                session.session.pickle()
            ))
        else:
            cursor.execute("update olmsessions set pickle=? where user = ? "
                           "and device_id = ? and identity_key = ? "
                           "and session_id = ?", (
                               session.session.pickle(),
                               session.user_id,
                               session.device_id,
                               session.identity_key,
                               session.session.id
                           ))

        self.database.commit()

        cursor.close()

    def save_inbound_group_session(self, room_id, session):
        # type: (str, InboundGroupSession) -> None
        cursor = self.database.cursor()

        cursor.execute("insert into inbound_group_sessions values(?,?,?)",
                       (room_id, session.id, session.pickle()))

        self.database.commit()

        cursor.close()

    def save_account(self, new=False):
        # type: (bool) -> None
        cursor = self.database.cursor()

        if new:
            cursor.execute("insert into olmaccount values (?,?)",
                           (self.user_id, self.account.pickle()))
        else:
            cursor.execute("update olmaccount set pickle=? where user = ?",
                           (self.account.pickle(), self.user_id))

        self.database.commit()
        cursor.close()

    @staticmethod
    def _check_db_tables(database):
        # type: (sqlite3.Connection) -> bool
        new = False
        cursor = database.cursor()
        cursor.execute("""select name from sqlite_master where type='table'
                          and name='olmaccount'""")
        if not cursor.fetchone():
            cursor.execute("create table olmaccount (user text, pickle text)")
            database.commit()
            new = True

        cursor.execute("""select name from sqlite_master where type='table'
                          and name='olmsessions'""")
        if not cursor.fetchone():
            cursor.execute("""create table olmsessions (user text,
                              device_id text, identity_key text,
                              session_id text, pickle text)""")
            database.commit()
            new = True

        cursor.execute("""select name from sqlite_master where type='table'
                          and name='inbound_group_sessions'""")
        if not cursor.fetchone():
            cursor.execute("""create table inbound_group_sessions
                              (room_id text, session_id text, pickle text)""")
            database.commit()
            new = True

        cursor.close()
        return new

    def sign_json(self, json_dict):
        # type: (Dict[Any, Any]) -> str
        signature = self.account.sign(self._to_json(json_dict))
        return signature

    @staticmethod
    def _to_json(json_dict):
        # type: (Dict[Any, Any]) -> str
        return json.dumps(
            json_dict,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True
        )

    def mark_keys_as_published(self):
        # type: () -> None
        self.account.mark_keys_as_published()