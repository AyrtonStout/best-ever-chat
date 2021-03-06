# -*- coding: utf-8 -*-
"""
    Best chat ever now a sockjs-tornado application.
"""
import StringIO
import json
import os
import random
import string
import time
from collections import deque
from hashlib import sha256

import sockjs.tornado
import tornado.web
import torndb
from boto3 import resource
from requests import get
from tornado.escape import to_unicode, linkify, xhtml_escape, url_unescape
from tornado.ioloop import IOLoop

from custom_render import BaseHandler, AuthLoginHandler, AuthCreateHandler, AuthLogoutHandler
from emoji.emojipy import Emoji

users = {}

MAX_DEQUE_LENGTH = 75

history = deque(maxlen=MAX_DEQUE_LENGTH)

emoji = Emoji()

client_version = 48
update_message = "<h3>I think you already know what's different.</h3>"

http_server = None

curated_emojis = [u'💩', u'😀', u'😁', u'😂', u'😃', u'😄', u'😅', u'😆', u'😉', u'😊', u'😋', u'😌', u'😍', u'😎',
                  u'😏', u'😐', u'😑', u'😒', u'😓', u'😔', u'😕', u'😖', u'😗', u'😘', u'😙', u'😚', u'😛', u'😜',
                  u'😝', u'😞', u'😟', u'😠', u'😡', u'😢', u'😣', u'😤', u'😥', u'😦', u'😧', u'😨', u'😩', u'😪',
                  u'😫', u'😬', u'😭', u'😮', u'😯', u'😰', u'😱', u'😲', u'😳', u'😴', u'😵', u'😶', u'😷', u'🙁',
                  u'🙂', u'🙃', u'🙄', u'🤖', u'🖳', u'💩', u'🚽', u'🚲', u'🐢', u'🐉', u'🐅', u'🐄', u'🐐', u'🐑',
                  u'🐬', u'🐳', u'💩', u'🖕', u'🖖', u'✌', u'🤘', u'🤙', u'🤚', u'🤛', u'🤜', u'🤝', u'🤞', u'💪',
                  u'🚀', u'🥓', u'🥒', u'🥞', u'🥔', u'🍌', u'🍍', u'🦄', u'🦈', u'💩']


class PageHandler(BaseHandler):
    """Regular HTTP handler to serve the chatroom page"""

    @tornado.web.authenticated
    def get(self):
        if self.current_user.username is not None:
            self.set_cookie('username', str(self.current_user.username))
        if self.current_user.color is not None:
            self.set_cookie('color', str(self.current_user.color))
        self.set_cookie('sounds', str(self.current_user.sound or 0))
        if self.current_user.soundSet is not None:
            self.set_cookie('sound_set', str(self.current_user.soundSet))
        self.render2('index.html', emoji_list=self.settings['emojis'])


class ValidateHandler(BaseHandler):
    @tornado.web.authenticated
    def post(self):
        new_name = self.get_argument('set_name', default=None, strip=True)
        if new_name is None or new_name == '':
            self.write(json.dumps(False))
            return
        if new_name == self.get_argument('username', default=None, strip=True):
            self.write(json.dumps(True))
            return
        self.write(json.dumps(new_name not in users.keys()))


class MultiRoomChatConnection(sockjs.tornado.SockJSConnection):
    """Chat connection implementation"""
    # Class level variable
    participants = set()
    rooms = {"main": []}
    username = None
    current_user = None
    previous_tell = None
    reply_to = None

    bucket = resource('s3').Bucket('best-ever-chat-image-cache')

    def on_open(self, info):
        parasite = self.session.handler.get_secure_cookie('parasite')
        if parasite is None:
            self.send_auth_fail()
            return False
        self.current_user = http_server.db.get("SELECT * FROM parasite WHERE id = %s", parasite)
        if not self.current_user:
            self.current_user = {}

        self.username = self.current_user.username

        # Add client to the clients list
        self.participants.add(self)
        if self.username not in users.keys():
            # Send that someone joined
            self.broadcast_from_server([x for x in self.participants if x.username != self.username],
                                       self.username + ' has connected')
            users[self.username] = {'color': self.current_user.color or '',
                                    'typing': False,
                                    'real_name': self.current_user.id}

        self.broadcast_user_list()
        self.send_chat_history()
        self.send_information(update_message)
        self.send_from_server('Connection successful')

    def on_message(self, message):
        json_message = json.loads(message)
        if self.current_user['id'] != self.session.handler.get_secure_cookie('parasite'):
            self.send_auth_fail()
        if json_message['type'] == 'chatMessage':
            if json_message['message'] and json_message['message'][0] == '/':
                self.parse_command(json_message)
            else:
                self.broadcast_chat_message(json_message['user'], json_message['message'])
        if json_message['type'] == 'version':
            if json_message['client_version'] < client_version:
                self.send_from_server('Your client is out of date. Refresh your page, you twit.')
                self.send({'type': 'versionUpdate'})
            if json_message['client_version'] > client_version:
                self.send_from_server('How did you mess up a perfectly good client version number?')
        if json_message['type'] == 'imageMessage':
            self.broadcast_image(json_message['user'], json_message['url'])
        if json_message['type'] == 'userSettings':
            self.update_user_settings(json_message['settings'])
        if json_message['type'] == 'userStatus':
            self.update_user_status(json_message['user'], json_message['status'])

    def on_close(self):
        # Remove client from the clients list and broadcast leave message
        self.participants.remove(self)
        if len([x for x in self.participants if x.username == self.username]) == 0:
            users.pop(self.username, None)
            self.broadcast_from_server(self.participants, self.username + " left.")
            self.broadcast_user_list()

        IOLoop.current().add_callback(self.close)

    def send_auth_fail(self):
        self.send({'type': 'auth_fail',
                   'data': {
                       'user': 'Server',
                       'message': 'You really messed something up. Please standby while I clean it up...',
                       'time': time.time()
                   }})

    def send_from_server(self, message):
        self.send({'type': 'chatMessage',
                   'data': {
                       'user': 'Server',
                       'message': message,
                       'time': time.time()}})

    def broadcast_from_server(self, send_to, message, message_type='chatMessage', data=None):
        new_message = {'user': 'Server',
                       'message': message,
                       'time': time.time(),
                       'data': data}
        self.broadcast(send_to, {'type': message_type,
                                 'data': new_message})

    def send_information(self, message):
        self.send({'type': 'information', 'data': {'message': message}})

    def broadcast_user_list(self):
        self.broadcast(self.participants, {'type': 'userList', 'data': users})

    def send_chat_history(self):
        self.send({'type': 'history', 'data': sorted(history, cmp=lambda x, y: cmp(x['time'], y['time']))})

    def broadcast_private_message(self, sender, recipient, message):
        sender_participants = [x for x in self.participants if x.current_user.id == sender]
        recipient_participants = [x for x in self.participants if x.current_user.id == recipient]
        for participant in recipient_participants:
            participant.reply_to = sender
        recipients = sender_participants + recipient_participants
        new_message = {'type': 'privateMessage', 'data': {'sender': sender_participants[0].username,
                                                          'recipient': recipient_participants[0].username,
                                                          'message': message,
                                                          'time': time.time()}}
        self.broadcast(recipients, new_message)

    def broadcast_chat_message(self, user, message):
        # first linkify
        message_text = linkify(to_unicode(message), extra_params='target="_blank"', require_protocol=False)
        # last find shortcode emojis
        message_text = emoji.shortcode_to_unicode(message_text)
        # then find ascii emojis
        message_text = emoji.ascii_to_unicode(message_text)

        new_message = {'user': user,
                       'color': users[user]['color'],
                       'message': message_text,
                       'time': time.time()}
        history.append(new_message)
        self.broadcast(self.participants, {'type': 'chatMessage',
                                           'data': new_message})

    def broadcast_image(self, user, image_url):
        s3_key = 'images/' + sha256(image_url).hexdigest()
        try:
            exists = filter(lambda x: x.key == s3_key, list(self.bucket.objects.all()))
            logging.info('Found object in S3: {}'.format(exists))
            if len(exists) <= 0:
                req_for_image = get(image_url, stream=True)
                file_object_from_req = req_for_image.raw
                req_data = file_object_from_req.read()
                if len(req_data) == 0:
                    raise Exception('empty data, response code:{}'.format(req_for_image.status_code))

                # Do the actual upload to s3
                self.bucket.put_object(Key=s3_key, Body=req_data, ACL='public-read')
            image_src_url = 'https://s3-us-west-2.amazonaws.com/best-ever-chat-image-cache/' + s3_key
        except Exception as e:
            logging.info(e.message)
            logging.info('Image failed to transfer to S3 bucket: URL({}) KEY({})'.format(image_url, s3_key))
            image_src_url = image_url

        new_message = {'user': user,
                       'color': users[user]['color'],
                       'message': "<a href=\"{}\" target=\"_blank\"><img src=\"{}\" width=\"300px\" /></a>".format(
                           xhtml_escape(image_url), xhtml_escape(image_src_url)),
                       'time': time.time()}
        history.append(new_message)
        self.broadcast(self.participants, {'type': 'chatMessage',
                                           'data': new_message})

    def update_user_settings(self, settings):
        updating_participants = [x for x in self.participants if x.current_user.id == self.current_user.id]

        if 'newSounds' in settings.keys():
            self.current_user.sound = settings['newSounds']
            self.broadcast_from_server(updating_participants,
                                       'Sounds {}.'.format(self.current_user.sound and 'enabled' or 'disabled'),
                                       message_type='update', data={'sounds': self.current_user.sound})
        if 'newSoundSet' in settings.keys():
            self.current_user.soundSet = settings['newSoundSet']
            self.broadcast_from_server(updating_participants, '{} sounds chosen.'.format(self.current_user.soundSet),
                                       message_type='update', data={'sound_set': self.current_user.soundSet})
        if 'newColor' in settings.keys():
            self.current_user.color = settings['newColor']
            users[self.username]['color'] = settings['newColor']
            self.broadcast_from_server(updating_participants, 'Color updated.', message_type='update',
                                       data={'color': self.current_user.color})

        if 'newUser' in settings.keys() and 'oldUser' in settings.keys():
            user = settings['oldUser']
            if settings['newUser'] in users.keys():  # username is taken, validation backup
                self.send({'type': 'revertName', 'data': self.username})
                self.send_from_server('Name already taken.')
            else:
                prev_user_data = users.pop(self.username, {})
                self.username = settings['newUser']
                self.current_user.username = self.username
                users[self.username] = prev_user_data

                self_set = {self}

                self.broadcast_from_server(self.participants.difference(self_set),
                                           user + " is now " + self.username)
                self.broadcast_from_server(updating_participants, "Name changed.", message_type='update',
                                           data={'username': self.username})
                self.broadcast_user_list()
        http_server.db.execute('UPDATE parasite SET color=%s, username=%s, sound=%s, soundSet=%s WHERE id=%s',
                               self.current_user.color, self.current_user.username, self.current_user.sound,
                               self.current_user.soundSet, self.current_user.id)

    def update_user_status(self, user, json_status):
        if user != self.username or not json_status:
            return

        if 'typing' in json_status:
            if 'currentMessage' in json_status and json_status['currentMessage']:
                typing_status = json_status['currentMessage'][0] != '/'
            else:
                typing_status = json_status['typing']

            if users[self.username]['typing'] is not typing_status:
                users[self.username]['typing'] = typing_status
                self.broadcast_user_list()

    def parse_command(self, json_message):
        message = json_message['message']
        command, _, command_args = message[1:].partition(' ')
        if command == 'help':
            self.send_from_server('Okay, fine, this is what you can do:<table>' +
                                  '<tr>' +
                                  '<td>/tell &lt;username&gt;</td>' +
                                  '<td>/t &lt;username&gt;</td>' +
                                  '<td>send a private message</td>' +
                                  '</tr>' +
                                  '<tr>' +
                                  '<td>/reply</td>' +
                                  '<td>/r</td>' +
                                  '<td>reply to the last private message you received</td>' +
                                  '</tr>' +
                                  '<tr>' +
                                  '<td>/retell</td>' +
                                  '<td>/rt</td>' +
                                  '<td>send a private message to the last person you sent one to</td>' +
                                  '</tr>' +
                                  # '<tr>' +
                                  # '<td>/create &lt;room name&gt;</td>' +
                                  # '<td>/c &lt;room name&gt;</td>' +
                                  # '<td>create a sub-room</td>' +
                                  # '</tr>' +
                                  # '<tr>' +
                                  # '<td>/invite &lt;username&gt;</td>' +
                                  # '<td>/i &lt;username&gt;</td>' +
                                  # '<td>invite someone to a sub-room you are in</td>' +
                                  # '</tr>' +
                                  # '<tr>' +
                                  # '<td>/leave &lt;room name&gt;</td>' +
                                  # '<td>/l &lt;room name&gt;</td>' +
                                  # '<td>leave a sub-room you are in</td>' +
                                  # '</tr>' +
                                  '</table>')
        # elif command == 'create':
        #     room_name = command_args.split(' ')[0]
        #     if room_name == '':
        #         self.send_from_server('You must supply a name to create a room.')
        #     else:
        #         if room_name in users.keys():
        #             self.send_from_server('You cannot use an existing username as a room name.')
        #         elif room_name in self.rooms.keys():
        #             self.send_from_server('Room \'{}\' already exists'.format(room_name))
        #         else:
        #             self.rooms[room_name] = [self.username]
        #             print self.rooms
        #             self.send_from_server('Created room \'{}\''.format(room_name))
        # elif command == 'invite':
        #     invitees = command_args.split(' ')
        #     room_name = invitees.pop(0)
        #     print invitees
        #     print room_name
        #     for user in invitees:
        #         if user not in users.keys():
        #             self.send_from_server('You cannot invite someone who is not connected to chat.')
        #         else:
        #             self.broadcast_from_server(
        #                 [x for x in self.participants if x.username in self.rooms[room_name]],
        #                 '{} has joined \'{}\''.format(user, room_name))
        #             self.rooms[room_name].append(user)
        #             self.broadcast_from_server([x for x in self.participants if x.username == user],
        #                                        'You have been added to \'{}\''.format(room_name))
        #     print self.username
        elif command == 'tell' or command == 't':
            user, _, message = command_args.partition(' ')
            if user == '':
                self.send_from_server('Who do you want to private message?<table><tr>' +
                                      '<td>/tell &lt;username&gt;</td>' +
                                      '<td>/t &lt;username&gt;</td>' +
                                      '</tr></table>')
            elif user in users.keys():
                self_participants = [x for x in self.participants if x.username == self.username]
                for participant in self_participants:
                    participant.previous_tell = users[user]['real_name']
                self.broadcast_private_message(self.current_user.id,
                                               self.previous_tell,
                                               message)
            else:
                self.send_from_server('{} is not connected to chat.'.format(user))
        elif command == 'retell' or command == 'rt':
            if self.previous_tell is not None:
                previous_tell_users = [x for x in users if users[x]['real_name'] == self.previous_tell]
                if len(previous_tell_users) == 0:
                    self.send_from_server('{} is not connected to chat.'.format(previous_tell_users[0]))
                else:
                    self.broadcast_private_message(self.current_user.id,
                                                   self.previous_tell,
                                                   command_args)
            else:
                self.send_from_server('You cannot retell if you have not sent a tell.')

        elif command == 'reply' or command == 'r':
            if self.reply_to is not None:
                reply_users = [x for x in users if users[x]['real_name'] == self.reply_to]
                if len(reply_users) == 0:
                    self.send_from_server('{} is not connected to chat.'.format(reply_users[0]))
                else:
                    self.broadcast_private_message(self.current_user.id,
                                                   self.reply_to,
                                                   command_args)
            else:
                self.send_from_server('You cannot reply if you have not received a tell.')
        else:
            # if command in self.rooms.keys():
            #     print command, 'is a room'
            # else:
            self.send_from_server('Invalid command \'{}\''.format(command))


class Application(tornado.web.Application):
    def __init__(self, handlers, settings):
        super(Application, self).__init__(handlers, **settings)
        # Have one global connection to the blog DB across all handlers
        self.db = torndb.Connection(
            host='127.0.0.1:3306',
            database='bestchat',
            user='bestChat',
            password='a5e625568329d8c2216631da90efc030121400bde3bde2300fd089b738568717'
        )


if __name__ == "__main__":
    import logging

    logging.getLogger().setLevel(logging.DEBUG)

    chat_router = sockjs.tornado.SockJSRouter(MultiRoomChatConnection, '/chat', {
        'disabled_transports': [
            'xhr',
            'xhr_streaming',
            'jsonp',
            'htmlfile',
            'eventsource'
        ]
    })
    settings = {
        'cookie_secret': ''.join(
            random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(32)),
        'template_path': 'templates',
        'xsrf_cookies': True,
        'login_url': "/login",
        'static_path': os.path.join(os.path.dirname(__file__), 'static'),
        'emojis': curated_emojis
    }
    handlers = [
                   (r"/", PageHandler),
                   (r"/register", AuthCreateHandler),
                   (r"/login", AuthLoginHandler),
                   (r"/logout", AuthLogoutHandler),
                   (r'/static/(.*)', {'path': settings['static_path']}),
                   ('/validate_username', ValidateHandler)
               ] + chat_router.urls

    http_server = Application(handlers, settings)
    http_server.listen(6969, no_keep_alive=True)

    tornado.ioloop.IOLoop.instance().start()
