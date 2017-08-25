# Best Chat Ever
It's, like, really good, oh my gosh.

## Setup:
### Install:
1. `pip install -r install/pip-install`


2. `npm install -g less less-plugin-clean-css`
#### Database:
1. Setup a local MySql database.
2. Import `install/bestChat.sql` to get the schema and tables.
3. Make a user 'bestChat' with the password in `tornado_chat.py` and (at least) the following
permissions to the `bestchat` schema: DELETE, EXECUTE, INSERT, SELECT, SHOW, VIEW, UPDATE


## To run:
1. `lessc ./static/css/chat.less ./static/chat.css --clean-css="--s1 --advanced --compatibility=ie8"`


2. `python tornado_chat.py`


---


favicon from: http://www.iconka.com


twemoji: https://github.com/twitter/twemoji


emojione: https://www.emojione.com
