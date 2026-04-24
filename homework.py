import logging
import os
import sys
import time
from logging import StreamHandler

from http import HTTPStatus

import requests
from dotenv import load_dotenv
from requests.exceptions import (ConnectionError, RequestException, Timeout)
from telebot import TeleBot
from telebot.apihelper import ApiException

load_dotenv()


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

handler = StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


PRACTICUM_TOKEN = os.getenv('VERIFICATION_CODE')
TELEGRAM_TOKEN = os.getenv('TOKEN')
TELEGRAM_CHAT_ID = os.getenv('CHAT_ID')

RETRY_PERIOD = 600
ENDPOINT = 'https://practicum.yandex.ru/api/user_api/homework_statuses/'
HEADERS = {'Authorization': f'OAuth {PRACTICUM_TOKEN}'}


HOMEWORK_VERDICTS = {
    'approved': 'Работа проверена: ревьюеру всё понравилось. Ура!',
    'reviewing': 'Работа взята на проверку ревьюером.',
    'rejected': 'Работа проверена: у ревьюера есть замечания.'
}


class APIConnectionError(Exception):
    """Исключение для ошибок соединения с API."""

    pass


class APIResponseError(Exception):
    """Exception for API response errors."""

    pass


class TelegramSendError(Exception):
    """Raised when message cannot be sent to Telegram."""

    pass


def check_tokens():
    """Checks the availability of environment variables."""
    if not PRACTICUM_TOKEN:
        logger.critical(
            'Missing required environment variable: VERIFICATION_CODE'
        )
        return False
    if not TELEGRAM_TOKEN:
        logger.critical('Missing required environment variable: TOKEN')
        return False
    if not TELEGRAM_CHAT_ID:
        logger.critical(
            'Missing required environment variable: CHAT_ID'
        )
        return False
    return True


def send_message(bot, message):
    """
    Sends a homework status notification to the user's Telegram chat.

    The function delivers status updates about reviewed homework to the chat
    specified by TELEGRAM_CHAT_ID environment variable.
    Typical messages include approval, rejection,
    or review start notifications.

    Args:
        bot (TeleBot): Instance of the TeleBot client.
        message (str): Text of the message to send.

    Raises:
        ApiTelegramException: If the Telegram API returns an error.
        Exception: For any other unexpected sending errors.

    Logs:
        DEBUG: When the message is sent successfully.
    """
    try:
        bot.send_message(TELEGRAM_CHAT_ID, message)

    except (ApiException, RequestException):
        return False

    except Exception:
        return False

    logger.debug('Successfully sent message.')
    return True


def get_api_answer(timestamp):
    """
    Fetches homework statuses from Practicum API since the given timestamp.

    Sends a GET request to the Practicum API endpoint with an OAuth token.
    The API returns a JSON object containing a list of homework reviews
    that have been updated after the specified timestamp.

    Args:
        timestamp (int): Unix timestamp for the 'from_date' parameter.
                          Used to get homework statuses after this time.

    Returns:
        dict: API response converted from JSON to a Python dictionary.

    Raises:
        APIConnectionError: If request timeout, network connection fails,
                            or any other request-related error occurs.
        APIResponseError: If the API returns an HTTP status code
                          other than 200.

    Logs:
        ERROR: Timeout, connection failure, request errors,
        or non-200 status codes.
    """
    payload = {'from_date': timestamp}
    try:
        response = requests.get(
            ENDPOINT, headers=HEADERS, params=payload, timeout=30
        )
    except Timeout:
        raise APIConnectionError('The API response timed out')

    except ConnectionError:
        raise APIConnectionError('Failed to connect to the API')

    except RequestException as e:
        raise APIConnectionError(f'Request failed: {e}')

    if response.status_code != HTTPStatus.OK:
        raise APIResponseError(
            f'The API returned the code {response.status_code}')

    return response.json()


def check_response(response):
    """Checks the API response against the documentation."""
    if not isinstance(response, dict):
        response_type = type(response).__name__
        error_msg = f'The API response is not a dictionary. Got {response_type} instead.'
        raise TypeError(error_msg)

    # control the presence of required keys
    for key in ['homeworks', 'current_date']:
        if key not in response:
            error_msg = f'The API response is missing a required key {key}'
            raise KeyError(error_msg)

    homeworks_list = response['homeworks']

    if not isinstance(homeworks_list, list):
        error_msg = 'The value of the key homeworks is not a list'
        raise TypeError(error_msg)

    # Return the homework list (may be empty)
    return homeworks_list


def parse_status(homework):
    """
    Extracts homework status and returns a formatted message.

    Args:
        homework (dict): A dictionary containing homework data.
                         Must have keys 'homework_name' and 'status'.

    Returns:
        str: Formatted message with homework name and corresponding verdict.

    Raises:
        KeyError: If 'homework_name' or 'status' key is missing.
        ValueError: If status value is not found in HOMEWORK_VERDICTS.

    Logs:
        ERROR: When required keys are missing or status is unexpected.
    """
    for key in ['homework_name', 'status']:
        if key not in homework:
            raise KeyError(f'Missing required key in homework dict: {key}')
    homework_name = homework['homework_name']
    status = homework['status']
    if status not in HOMEWORK_VERDICTS:
        raise ValueError(f'Unexpected homework status: {status}')
    verdict = HOMEWORK_VERDICTS[status]

    return (
        'Изменился статус проверки работы "{}". {}'
        .format(homework_name, verdict)
    )


def handle_error(error, bot, last_error_message):
    """Handle exceptions and send Telegram alert if it's a new error."""
    error_msg = f'Data processing error: {error}'
    logger.error(error_msg)

    if last_error_message != error_msg:
        sent = send_message(bot, error_msg)
        if not sent:
            raise TelegramSendError('Couldn"t send error message to Telegram.')
        return error_msg

    return last_error_message


def process_iteration(bot, last_status, timestamp):
    """Perform one iteration of homework status check."""
    response_dict = get_api_answer(timestamp)
    homework_list = check_response(response_dict)

    if not homework_list:
        logger.debug('There are no new homework statuses.')
        return last_status, timestamp

    last_homework = homework_list[-1]
    current_status = last_homework.get('status')

    if current_status != last_status:
        last_status = current_status
        message = parse_status(last_homework)
        sent = send_message(bot, message)
        if not sent:
            raise TelegramSendError('Couldn"t send error message to Telegram.')
    return last_status, timestamp


def main():
    """The basic logic of the bot's operation."""
    if not check_tokens():
        logging.critical('Required environment variables are missing!')
        return

    # Create a bot class object.
    bot = TeleBot(token=TELEGRAM_TOKEN)

    last_status = None
    timestamp = int(time.time())
    last_error_message = None  # To control duplicate errors in Telegram

    while True:
        timestamp = int(time.time())

        try:
            last_status, timestamp = process_iteration(
                bot, last_status, timestamp
            )

            if last_status == 'approved':
                logger.info('Работа сдана! Завершаем работу бота.')
                break

        except (KeyError, TypeError, ValueError) as e:
            last_error_message = handle_error(e, bot, last_error_message)

        except (APIConnectionError, APIResponseError) as e:
            last_error_message = handle_error(e, bot, last_error_message)

        except (ConnectionError, TimeoutError) as e:
            logger.error(f'Connection error: {e}')

        except TelegramSendError:
            logger.error('Couldn"t send error message to Telegram.')

        except Exception as e:
            last_error_message = handle_error(e, bot, last_error_message)

        time.sleep(RETRY_PERIOD)


if __name__ == '__main__':
    """
    Main bot logic: fetches homework statuses and sends Telegram notifications.

    The bot runs in an infinite loop, checking the Practicum API every
    10 minutes for homework status updates. When a status changes, it sends
    a notification to the user's Telegram chat. The bot stops when the
    homework status becomes 'approved'.

    Environment variables (VERIFICATION_CODE, TOKEN, CHAT_ID) must be set.

    Handles:
        - Missing environment variables (CRITICAL)
        - API request errors (ERROR, with Telegram alert for new errors)
        - Data structure errors (ERROR, with Telegram alert)
        - Empty status lists (DEBUG, no alert)

    Logs:
        INFO: Normal operation, approved status reached.
        DEBUG: Empty status list, successful message sending.
        ERROR: API errors, data errors, sending failures.
        CRITICAL: Missing required environment variables.
    """
    main()
