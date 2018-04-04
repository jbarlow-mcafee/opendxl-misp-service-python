from __future__ import absolute_import
import logging
import os
import threading
import zmq

from dxlbootstrap.app import Application
from dxlclient import ServiceRegistrationInfo
from dxlclient.message import Event
from pymisp import PyMISP
from dxlmispservice._requesthandlers import MispServiceRequestCallback

# Configure local logger
logger = logging.getLogger(__name__)


class MispService(Application):
    """
    The "MISP DXL service library" application class.
    """

    _SERVICE_BASE_NAME = "/opendxl-misp/service"
    _SERVICE_TYPE = _SERVICE_BASE_NAME + "/misp-api"

    _GENERAL_CONFIG_SECTION = "General"
    _GENERAL_SERVICE_UNIQUE_ID_PROP = "serviceUniqueId"
    _GENERAL_HOST_CONFIG_PROP = "host"
    _GENERAL_API_PORT_CONFIG_PROP = "apiPort"
    _GENERAL_API_KEY_CONFIG_PROP = "apiKey"
    _GENERAL_API_NAMES_CONFIG_PROP = "apiNames"
    _GENERAL_CLIENT_CERTIFICATE_CONFIG_PROP = "clientCertificate"
    _GENERAL_CLIENT_KEY_CONFIG_PROP = "clientKey"
    _GENERAL_VERIFY_CERTIFICATE_CONFIG_PROP = "verifyCertificate"
    _GENERAL_VERIFY_CERT_BUNDLE_CONFIG_PROP = "verifyCertBundle"
    _GENERAL_ZEROMQ_PORT_CONFIG_PROP = "zeroMqPort"
    _GENERAL_ZEROMQ_NOTIFICATION_TOPICS_CONFIG_PROP = "zeroMqNotificationTopics"

    _DEFAULT_API_PORT = 443
    _DEFAULT_ZEROMQ_PORT = 50000

    _ZEROMQ_NOTIFICATIONS_EVENT_TOPIC = _SERVICE_BASE_NAME + \
                                        "/zeromq-notifications"

    def __init__(self, config_dir):
        """
        Constructor parameters:

        :param config_dir: The location of the configuration files for the
            application
        """
        super(MispService, self).__init__(config_dir, "dxlmispservice.config")
        self.__lock = threading.RLock()
        self.__destroyed = False
        self._service_unique_id = None
        self._api_client = None
        self._api_names = ()
        self._pymisp_client = None
        self._zeromq_socket = None
        self._zeromq_thread = None

    @property
    def client(self):
        """
        The DXL client used by the application to communicate with the DXL
        fabric
        """
        return self._dxl_client

    @property
    def config(self):
        """
        The application configuration (as read from the "dxlmispservice.config" file)
        """
        return self._config

    def on_run(self):
        """
        Invoked when the application has started running.
        """
        logger.info("On 'run' callback.")

    def _get_setting_from_config(self, section, setting,
                                 default_value=None,
                                 return_type=str,
                                 raise_exception_if_missing=False,
                                 is_file_path=False):
        """
        Get the value for a setting in the application configuration file.

        :param str section: Name of the section in which the setting resides.
        :param str setting: Name of the setting.
        :param default_value: Value to return if the setting is not found in
            the configuration file.
        :param type return_type: Expected 'type' of the value to return.
        :param bool raise_exception_if_missing: Whether or not to raise an
            exception if the setting is missing from the configuration file.
        :param bool is_file_path: Whether or not the value for the setting
            represents a file path. If set to 'True' but a file cannot be
            found for the setting, a ValueError is raised.
        :return: Value for the setting.
        :raises ValueError: If the setting cannot be found in the configuration
            file and 'raise_exception_if_missing' is set to 'True', the
            type of the setting found in the configuration file does not
            match the value specified for 'return_type', or 'is_file_path' is
            set to 'True' but no file can be found which matches the value
            read for the setting.
        """
        config = self.config
        if config.has_option(section, setting):
            getter_methods = {str: config.get,
                              list: config.get,
                              bool: config.getboolean,
                              int: config.getint,
                              float: config.getfloat}
            try:
                return_value = getter_methods[return_type](section, setting)
            except ValueError as ex:
                raise ValueError(
                    "Unexpected value for setting {} in section {}: {}".format(
                        setting, section, ex))
            if return_type == str:
                return_value = return_value.strip()
                if len(return_value) is 0 and raise_exception_if_missing:
                    raise ValueError(
                        "Required setting {} in section {} is empty".format(
                            setting, section))
            elif return_type == list:
                return_value = [item.strip()
                                for item in return_value.split(",")]
                if len(return_value) is 1 and len(return_value[0]) is 0 \
                        and raise_exception_if_missing:
                    raise ValueError(
                        "Required setting {} in section {} is empty".format(
                            setting, section))
        elif raise_exception_if_missing:
            raise ValueError(
                "Required setting {} not found in {} section".format(setting,
                                                                     section))
        else:
            return_value = default_value

        if is_file_path and return_value:
            return_value = self._get_path(return_value)
            if not os.path.isfile(return_value):
                raise ValueError(
                    "Cannot find file for setting {} in section {}: {}".format(
                        setting, section, return_value))

        return return_value

    def on_load_configuration(self, config):
        """
        Invoked after the application-specific configuration has been loaded

        This callback provides the opportunity for the application to parse
        additional configuration properties.

        :param config: The application configuration
        """
        logger.info("On 'load configuration' callback.")

        self._service_unique_id = self._get_setting_from_config(
            self._GENERAL_CONFIG_SECTION,
            self._GENERAL_SERVICE_UNIQUE_ID_PROP)

        host = self._get_setting_from_config(
            self._GENERAL_CONFIG_SECTION,
            self._GENERAL_HOST_CONFIG_PROP,
            raise_exception_if_missing=True)

        api_port = self._get_setting_from_config(
            self._GENERAL_CONFIG_SECTION,
            self._GENERAL_API_PORT_CONFIG_PROP,
            return_type=int,
            default_value=self._DEFAULT_API_PORT)

        self._api_names = self._get_setting_from_config(
            self._GENERAL_CONFIG_SECTION,
            self._GENERAL_API_NAMES_CONFIG_PROP,
            return_type=list,
            default_value=[])

        if self._api_names:
            api_key = self._get_setting_from_config(
                self._GENERAL_CONFIG_SECTION,
                self._GENERAL_API_KEY_CONFIG_PROP,
                raise_exception_if_missing=True
            )
            api_url = "https://{}:{}".format(host, api_port)

            verify_certificate = self._get_setting_from_config(
                self._GENERAL_CONFIG_SECTION,
                self._GENERAL_VERIFY_CERTIFICATE_CONFIG_PROP,
                return_type=bool,
                default_value=True
            )
            if verify_certificate:
                verify_cert_bundle = self._get_setting_from_config(
                    self._GENERAL_CONFIG_SECTION,
                    self._GENERAL_VERIFY_CERT_BUNDLE_CONFIG_PROP,
                    is_file_path=True
                )
                if verify_cert_bundle:
                    verify_certificate = verify_cert_bundle

            client_certificate = self._get_setting_from_config(
                self._GENERAL_CONFIG_SECTION,
                self._GENERAL_CLIENT_CERTIFICATE_CONFIG_PROP,
                is_file_path=True
            )
            client_key = self._get_setting_from_config(
                self._GENERAL_CONFIG_SECTION,
                self._GENERAL_CLIENT_KEY_CONFIG_PROP,
                is_file_path=True
            )

            if client_certificate:
                if client_key:
                    cert = (client_certificate, client_key)
                else:
                    cert = client_certificate
            else:
                cert = None

            logger.info("Connecting to API URL: %s", api_url)
            self._api_client = PyMISP(api_url, api_key,
                                      ssl=verify_certificate, cert=cert)

        self._zeromq_notification_topics = self._get_setting_from_config(
            self._GENERAL_CONFIG_SECTION,
            self._GENERAL_ZEROMQ_NOTIFICATION_TOPICS_CONFIG_PROP,
            return_type=list,
            default_value=[])

        if self._zeromq_notification_topics:
            zeromq_port = self._get_setting_from_config(
                self._GENERAL_CONFIG_SECTION,
                self._GENERAL_ZEROMQ_PORT_CONFIG_PROP,
                default_value=self._DEFAULT_ZEROMQ_PORT,
                return_type=int
            )
            self._start_zeromq_listener(host, zeromq_port)

    def _start_zeromq_listener(self, host, port):
        context = zmq.Context()
        self._zeromq_socket = context.socket(zmq.SUB)
        socket_url = "tcp://%s:%s" % (host, port)
        logger.info("Connecting to zeromq URL: %s", socket_url)
        self._zeromq_socket.connect(socket_url)
        for topic in self._zeromq_notification_topics:
            logger.debug("Subscribing to zeromq topic: %s", topic)
            self._zeromq_socket.subscribe(topic)
        logger.info("Waiting for zeromq notifications: %s",
                    self._zeromq_notification_topics)

        self._zeromq_poller = zmq.Poller()
        self._zeromq_poller.register(self._zeromq_socket, zmq.POLLIN)
        zeromq_thread = threading.Thread(
            target=self._process_zeromq_messages)
        zeromq_thread.daemon = True
        self._zeromq_thread = zeromq_thread
        self._zeromq_thread.start()

    def _process_zeromq_messages(self):
        while not self.__destroyed:
            try:
                socks = dict(self._zeromq_poller.poll(timeout=None))
            except zmq.ZMQError:
                socks = {}
            if self._zeromq_socket in socks and \
                socks[self._zeromq_socket] == zmq.POLLIN:
                message = self._zeromq_socket.recv_string()
                topic, _, payload = message.partition(" ")
                logger.debug("Received notification for %s", topic)
                full_event_topic = "{}{}/{}".format(
                    self._ZEROMQ_NOTIFICATIONS_EVENT_TOPIC,
                    "/{}".format(self._service_unique_id)
                    if self._service_unique_id else "",
                    topic)
                event = Event(full_event_topic)
                logger.debug("Forwarding notification to %s ...",
                             full_event_topic)
                event.payload = payload
                self.client.send_event(event)

    def destroy(self):
        super(MispService, self).destroy()
        with self.__lock:
            if not self.__destroyed:
                self.__destroyed = True
                if self._zeromq_socket:
                    logger.debug("Closing zeromq socket ...")
                    self._zeromq_socket.close()
                if self._zeromq_thread:
                    logger.debug(
                        "Waiting for zeromq message thread to terminate ...")
                    self._zeromq_thread.join()
                    logger.debug("Zeromq message thread terminated")

    def on_dxl_connect(self):
        """
        Invoked after the client associated with the application has connected
        to the DXL fabric.
        """
        logger.info("On 'DXL connect' callback.")

    def _get_api_method(self, api_name):
        """
        Retrieve an instance method from the PyMISP API client object.

        :param str api_name: String name of the instance method object to
            retrieve from the PyMISP client object.
        :return: Matching instancemethod if available, else None.
        :rtype: instancemethod
        """
        api_method = None
        if hasattr(self._api_client, api_name):
            api_attr = getattr(self._api_client, api_name)
            if callable(api_attr):
                api_method = api_attr
        return api_method

    def on_register_services(self):
        """
        Invoked when services should be registered with the application
        """
        api_methods = []
        for api_name in self._api_names:
            api_method = self._get_api_method(api_name)
            if api_method:
                api_methods.append(api_method)
            else:
                logger.warning("MISP API name is invalid: %s",
                               api_name)

        if api_methods:
            logger.info("Registering service: misp_service")
            service = ServiceRegistrationInfo(
                self._dxl_client,
                self._SERVICE_TYPE)

            for api_method in api_methods:
                api_method_name = api_method.__name__
                topic = "{}{}/{}".format(
                    self._SERVICE_TYPE,
                    "/{}".format(self._service_unique_id)
                    if self._service_unique_id else "",
                    api_method_name)
                logger.info(
                    "Registering request callback: %s%s_%s_%s. Topic: %s.",
                    "misp",
                    "_{}".format(self._service_unique_id)
                    if self._service_unique_id else "",
                    api_method_name,
                    "requesthandler",
                    topic)
                self.add_request_callback(
                    service,
                    topic,
                    MispServiceRequestCallback(self, api_method),
                    False)

            self.register_service(service)
