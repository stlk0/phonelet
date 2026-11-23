import pytest
from phonelet.config import Settings


def test_check_secrets_rejects_default_ami_secret():
    s = Settings(ami_secret="changeme", api_key="real-key")
    with pytest.raises(ValueError, match="PHONELET_AMI_SECRET"):
        s.check_secrets()


def test_check_secrets_rejects_default_api_key():
    s = Settings(ami_secret="real-secret", api_key="changeme")
    with pytest.raises(ValueError, match="PHONELET_API_KEY"):
        s.check_secrets()


def test_check_secrets_rejects_empty_values():
    s = Settings(ami_secret="", api_key="")
    with pytest.raises(ValueError, match="PHONELET_AMI_SECRET"):
        s.check_secrets()


def test_check_secrets_passes_with_real_values():
    s = Settings(ami_secret="real-secret", api_key="real-key")
    s.check_secrets()  # should not raise


@pytest.mark.parametrize('value', ['GENERATE_STRONG_SECRET_HERE', ' generate_key ', 'CHANGE_ME', 'REPLACE_ME', 'YOUR_TOKEN'])
def test_reject_example_placeholders(value):
    with pytest.raises(ValueError):
        Settings(ami_secret=value, api_key='real-key').check_secrets()


@pytest.mark.parametrize('name', ['modem_poll_interval', 'sms_reassembly_timeout',
                                 'ami_reconnect_timeout', 'ingress_poll_interval', 'min_free_bytes',
                                 'wifi_connect_timeout'])
@pytest.mark.parametrize('value', [0, -1])
def test_positive_settings(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


@pytest.mark.parametrize('name', ['unknown_setting', 'example_interval'])
def test_unknown_settings_are_refused(name):
    # The deploy preflight reads .env with these kwargs: an unknown line stops it before any change.
    with pytest.raises(ValueError, match='Extra inputs are not permitted'):
        Settings(**{name: 10})


@pytest.mark.parametrize('kwargs', [dict(telegram_token='token'), dict(telegram_chat_id='id'),
                                   dict(ha_url='http://ha'), dict(ha_token='token'),
                                   dict(ha_url='ftp://ha', ha_token='token'), dict(ami_port=65536)])
def test_invalid_related_configuration(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_logging_normalized():
    assert Settings(log_level='debug').log_level == 'DEBUG'


def test_validation_errors_do_not_expose_credentials():
    with pytest.raises(ValueError) as caught:
        Settings(ami_secret='private-secret', telegram_token='private-token')
    assert 'private-secret' not in str(caught.value)
    assert 'private-token' not in str(caught.value)


@pytest.mark.parametrize('value', [float('inf'), float('nan')])
def test_intervals_are_finite(value):
    with pytest.raises(ValueError):
        Settings(ingress_poll_interval=value)


def test_incomplete_multipart_sms_waits_ten_minutes_by_default(monkeypatch):
    monkeypatch.delenv('PHONELET_SMS_REASSEMBLY_TIMEOUT', raising=False)
    assert Settings().sms_reassembly_timeout == 600


def test_wifi_interface_must_be_a_plain_name():
    with pytest.raises(ValueError, match='Wi-Fi interface'):
        Settings(wifi_interface='wlan0; reboot')


def test_sip_message_destination_default_and_disabled(monkeypatch):
    monkeypatch.delenv('PHONELET_SIP_MESSAGE_DESTINATION', raising=False)
    assert Settings().sip_message_destination == 'pjsip:phone'
    monkeypatch.setenv('PHONELET_SIP_MESSAGE_DESTINATION', '')
    assert Settings().sip_message_destination == ''


@pytest.mark.parametrize('destination', ['phone', 'pjsip:', ' ', 'pjsip:phone\r\nAction: Ping',
                                         'pjsip:phone;invalid'])
def test_invalid_nonempty_sip_message_destination(destination):
    with pytest.raises(ValueError, match='Invalid SIP message destination'):
        Settings(sip_message_destination=destination)


def test_invalid_api_host_error_does_not_echo_input():
    with pytest.raises(ValueError) as caught:
        Settings(api_host='private-invalid-host')
    assert 'Invalid PHONELET_API_HOST' in str(caught.value)
    assert 'private-invalid-host' not in str(caught.value)
