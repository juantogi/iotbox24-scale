# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
import os
import re
import time

from collections import namedtuple
from os import listdir
from threading import Thread, Lock

from odoo import http

from odoo.addons.hw_proxy.controllers import main as hw_proxy

_logger = logging.getLogger(__name__)

DRIVER_NAME = 'scale'

try:
    import serial
except ImportError:
    _logger.error('Odoo module hw_scale depends on the pyserial python module')
    serial = None


def _toledo8217StatusParse(status):
    """ Parse a scale's status, returning a `(weight, weight_info)` pair. """
    weight, weight_info = None, None
    stat = status[status.index(b'?') + 1]
    if stat == 0:
        weight_info = 'ok'
    else:
        weight_info = []
        if stat & 1 :
            weight_info.append('moving')
        if stat & 1 << 1:
            weight_info.append('over_capacity')
        if stat & 1 << 2:
            weight_info.append('negative')
            weight = 0.0
        if stat & 1 << 3:
            weight_info.append('outside_zero_capture_range')
        if stat & 1 << 4:
            weight_info.append('center_of_zero')
        if stat & 1 << 5:
            weight_info.append('net_weight')
    return weight, weight_info

ScaleProtocol = namedtuple(
    'ScaleProtocol',
    "name baudrate bytesize stopbits parity timeout writeTimeout weightRegexp statusRegexp "
    "statusParse commandTerminator commandDelay weightDelay newWeightDelay disable "
    "weightCommand zeroCommand tareCommand clearCommand emptyAnswerValid autoResetWeight")

# 8217 Mettler-Toledo (Weight-only) Protocol, as described in the scale's Service Manual.
#    e.g. here: https://www.manualslib.com/manual/861274/Mettler-Toledo-Viva.html?page=51#manual
# Our recommended scale, the Mettler-Toledo "Ariva-S", supports this protocol on
# both the USB and RS232 ports, it can be configured in the setup menu as protocol option 3.
# We use the default serial protocol settings, the scale's settings can be configured in the
# scale's menu anyway.
Toledo8217Protocol = ScaleProtocol(
    name='Toledo 8217',
    baudrate=9600,
    bytesize=serial.SEVENBITS,
    stopbits=serial.STOPBITS_ONE,
    parity=serial.PARITY_EVEN,
    timeout=1,
    writeTimeout=1,
    weightRegexp=b"\x02\\s*([0-9.]+)N?\\r",
    statusRegexp=b"\x02\\s*(\\?.)\\r",
    statusParse=_toledo8217StatusParse,
    commandDelay=0.2,
    weightDelay=0.5,
    newWeightDelay=0.2,
    commandTerminator=b'',
    weightCommand=b'W',
    zeroCommand=b'Z',
    tareCommand=b'T',
    clearCommand=b'C',
    emptyAnswerValid=False,
    autoResetWeight=False,
    disable=False
)

# The ADAM scales have their own RS232 protocol, usually documented in the scale's manual
#   e.g at https://www.adamequipment.com/media/docs/Print%20Publications/Manuals/PDF/AZEXTRA/AZEXTRA-UM.pdf
#          https://www.manualslib.com/manual/879782/Adam-Equipment-Cbd-4.html?page=32#manual
# Only the baudrate and label format seem to be configurable in the AZExtra series.
ADAMEquipmentProtocol = ScaleProtocol(
    name='Adam Equipment',
    baudrate=4800,
    bytesize=serial.EIGHTBITS,
    stopbits=serial.STOPBITS_ONE,
    parity=serial.PARITY_NONE,
    timeout=0.2,
    writeTimeout=0.2,
    weightRegexp=b"\s*([0-9.]+)kg", # LABEL format 3 + KG in the scale settings, but Label 1/2 should work
    statusRegexp=None,
    statusParse=None,
    commandTerminator=b"\r\n",
    commandDelay=0.2,
    weightDelay=0.5,
    newWeightDelay=5,  # AZExtra beeps every time you ask for a weight that was previously returned!
                       # Adding an extra delay gives the operator a chance to remove the products
                       # before the scale starts beeping. Could not find a way to disable the beeps.
    weightCommand=b'P',
    zeroCommand=b'Z',
    tareCommand=b'T',
    clearCommand=None, # No clear command -> Tare again
    emptyAnswerValid=True, # AZExtra does not answer unless a new non-zero weight has been detected
    autoResetWeight=True,  # AZExtra will not return 0 after removing products
    disable=True
)


SCALE_PROTOCOLS = (
    Toledo8217Protocol,
    ADAMEquipmentProtocol, # must be listed last, as it supports no probing!
)


def getPeso():
    s4 = "0.00"
    while True:
        while True: 
            try:
                if ( os.path.exists('/dev/ttyUSB0')):
                    ser = serial.Serial('/dev/ttyUSB0', 9600)
                    s = ser.read(100)
                    break
                elif ( os.path.exists('/dev/ttyUSB1')):
                    ser = serial.Serial('/dev/ttyUSB1', 9600)
                    s = ser.read(100)
                    break
                else:
                    print ('No ha conectado el cable USB - FTDI a la Balanza o está apagada')
            except serial.SerialException:
                print ("ERROR - SerialException el USB - FTDI a la Balanza")


        try:
            s2 = s.decode("utf-8") ##Convertimos de Bytes a String
            inicio = s2.find("\x02")  # guarda posicion de x02 
            fin = s2.find("\r")  # guarda posicion de r
            s3 = s2[inicio+3:fin] # extraemos la medida
            s4 = s3.strip() # le quitamos los espacios en blanco
            if (fin > inicio):
                break  

        except Exception:
            print ("Excepcion Controlada")

    return s4

class Scale(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.lock = Lock()
        self.scalelock = Lock()
        self.status = {'status':'connecting', 'messages':[]}
        self.input_dir = '/dev/serial/by-path/'
        self.forbidden_dir = '/dev/serial/by-id/'
        self.weight = 0
        self.weight_info = 'ok'
        self.device = None
        self.path_to_scale = ''
        self.protocol = None
        self.disabled = False

    def lockedstart(self):
        with self.lock:
            if not self.isAlive():
                self.daemon = True
                self.start()

    def set_status(self, status, message=None):
        if status == self.status['status']:
            if message is not None and message != self.status['messages'][-1]:
                self.status['messages'].append(message)

                if status == 'error' and message:
                    _logger.error('Scale Error: '+ message)
                elif status == 'disconnected' and message:
                    _logger.warning('Disconnected Scale: '+ message)
        else:
            self.status['status'] = status
            if message:
                self.status['messages'] = [message]
            else:
                self.status['messages'] = []

            if status == 'error' and message:
                _logger.error('Scale Error: '+ message)
            elif status == 'disconnected' and message:
                _logger.info('Disconnected Scale: %s', message)

    def _get_raw_response(self, connection):
        answer = []
        while True:
            char = connection.read(1) # may return `bytes` or `str`
            if not char:
                break
            else:
                answer.append(bytes(char))
        return b''.join(answer)

    def _parse_weight_answer(self, protocol, answer):
        """ Parse a scale's answer to a weighing request, returning
            a `(weight, weight_info, status)` pair.
        """
        weight, weight_info, status = None, None, None
        try:
            _logger.debug("Parsing weight [%r]", answer)
            if not answer and protocol.emptyAnswerValid:
                # Some scales do not return the same value again, but we
                # should not clear the weight data, POS may still be reading it
                return weight, weight_info, status

            if protocol.statusRegexp and re.search(protocol.statusRegexp, answer):
                # parse status to set weight_info - we'll try weighing again later
                weight, weight_info = protocol.statusParse(answer)
            else:
                match = re.search(protocol.weightRegexp, answer)
                if match:
                    weight_text = match.group(1)
                    try:
                        weight = float(weight_text)
                        _logger.info('Weight: %s', weight)
                    except ValueError:
                        _logger.exception("Cannot parse weight [%r]", weight_text)
                        status = 'Invalid weight, please power-cycle the scale'
                else:
                    _logger.error("Cannot parse scale answer [%r]", answer)
                    status = 'Invalid scale answer, please power-cycle the scale'
        except Exception as e:
            _logger.exception("Cannot parse scale answer [%r]", answer)
            status = ("Could not weigh on scale %s with protocol %s: %s" %
                      (self.path_to_scale, protocol.name, e))
        return weight, weight_info, status

    def get_device(self):
        if self.device:
            return self.device

        with hw_proxy.rs232_lock:
            try:
                if not os.path.exists(self.input_dir):
                    self.set_status('disconnected', 'No RS-232 device found')
                    return None

                forbidden_devices = [os.readlink(self.forbidden_dir + d) for d in listdir(self.forbidden_dir) if 'usb-Sylvac_Power_USB_A32DV5VM' in d] # Skip special usb link with Sylvac
                devices = [device for device in listdir(self.input_dir) if os.readlink(self.input_dir + device) not in forbidden_devices]
                for device in devices:
                    path = self.input_dir + device
                    driver = hw_proxy.rs232_devices.get(device)
                    if driver and driver != DRIVER_NAME:
                        # belongs to another driver
                        _logger.info('Ignoring %s, belongs to %s', device, driver)
                        continue

                    for protocol in SCALE_PROTOCOLS:
                        _logger.info('Probing %s with protocol %s', path, protocol)
                        connection = serial.Serial(path,
                                                   baudrate=protocol.baudrate,
                                                   bytesize=protocol.bytesize,
                                                   stopbits=protocol.stopbits,
                                                   parity=protocol.parity,
                                                   timeout=1,      # longer timeouts for probing
                                                   writeTimeout=1) # longer timeouts for probing
                        connection.write(protocol.weightCommand + protocol.commandTerminator)
                        time.sleep(protocol.commandDelay)
                        answer = self._get_raw_response(connection)
                        weight, weight_info, status = self._parse_weight_answer(protocol, answer)
                        if status:
                            _logger.info('Probing %s: no valid answer to protocol %s', path, protocol.name)
                        else:
                            _logger.info('Probing %s: answer looks ok for protocol %s', path, protocol.name)
                            self.path_to_scale = path
                            self.protocol = protocol
                            self.set_status(
                                'connected',
                                'Connected to %s with %s protocol' % (device, protocol.name)
                            )
                            connection.timeout = protocol.timeout
                            connection.writeTimeout = protocol.writeTimeout
                            hw_proxy.rs232_devices[path] = DRIVER_NAME
                            return connection

                self.set_status('disconnected', 'No supported RS-232 scale found')
            except Exception as e:
                _logger.exception('Failed probing for scales')
                self.set_status('error', 'Failed probing for scales: %s' % e)
            return None

    def get_weight(self):
        self.repeats = 5
        self.disabled = False
        self.lockedstart()
        return self.weight

    def get_weight_info(self):
        self.lockedstart()
        return self.weight_info

    def get_status(self):
        self.lockedstart()
        return self.status

    def read_weight(self):
        with self.scalelock:
            p = self.protocol
            try:
                self.device.write(p.weightCommand + p.commandTerminator)
                time.sleep(p.commandDelay)
                answer = self._get_raw_response(self.device)
                weight, weight_info, status = self._parse_weight_answer(p, answer)
                if status:
                    self.set_status('error', status)
                    self.device = None
                else:
                    if weight is not None:
                        self.weight = weight
                    if weight_info is not None:
                        self.weight_info = weight_info
            except Exception as e:
                self.set_status(
                    'error',
                    "Could not weigh on scale %s with protocol %s: %s" %
                    (self.path_to_scale, p.name, e))
                self.device = None

    def set_zero(self):
        with self.scalelock:
            if self.device:
                try:
                    self.device.write(self.protocol.zeroCommand + self.protocol.commandTerminator)
                    time.sleep(self.protocol.commandDelay)
                except Exception as e:
                    self.set_status(
                        'error',
                        "Could not zero scale %s with protocol %s: %s" %
                        (self.path_to_scale, self.protocol.name, e))
                    self.device = None

    def set_tare(self):
        with self.scalelock:
            if self.device:
                try:
                    self.device.write(self.protocol.tareCommand + self.protocol.commandTerminator)
                    time.sleep(self.protocol.commandDelay)
                except Exception as e:
                    self.set_status(
                        'error',
                        "Could not tare scale %s with protocol %s: %s" %
                        (self.path_to_scale, self.protocol.name, e))
                    self.device = None

    def clear_tare(self):
        with self.scalelock:
            if self.device:
                p = self.protocol
                try:
                    # if the protocol has no clear, we can just tare again
                    clearCommand = p.clearCommand or p.tareCommand
                    self.device.write(clearCommand + p.commandTerminator)
                    time.sleep(p.commandDelay)
                except Exception as e:
                    self.set_status(
                        'error',
                        "Could not clear tare on scale %s with protocol %s: %s" %
                        (self.path_to_scale, p.name, e))
                    self.device = None

    def run(self):
        self.device = None

        while True:
            if not self.disabled:
                if self.device:
                    old_weight = self.weight
                    self.read_weight()
                    if self.weight != old_weight:
                        _logger.info('New Weight: %s, sleeping %ss', self.weight, self.protocol.newWeightDelay)
                        time.sleep(self.protocol.newWeightDelay)
                        if self.weight and self.protocol.autoResetWeight:
                            self.weight = 0
                    else:
                        _logger.info('Weight: %s, sleeping %ss', self.weight, self.protocol.weightDelay)
                        time.sleep(self.protocol.weightDelay)
                        self.disabled = True
                else:
                    with self.scalelock:
                        self.device = self.get_device()
                    if not self.device:
                        # retry later to support "plug and play"
                        time.sleep(10)
                    else: 
                        self.disabled = self.protocol.disable
            else:
                time.sleep(10)


scale_thread = None
if serial:
    scale_thread = Scale()
    hw_proxy.drivers[DRIVER_NAME] = scale_thread

class ScaleDriver(hw_proxy.Proxy):
    @http.route('/hw_proxy/scale_read/', type='json', auth='none', cors='*')
    def scale_read(self):
        if scale_thread:
            return {'weight': float(getPeso()),
                    'unit': 'kg',
                    'info': 'ok'}
        return None

    @http.route('/hw_proxy/scale_zero/', type='json', auth='none', cors='*')
    def scale_zero(self):
        if scale_thread:
            scale_thread.set_zero()
        return True

    @http.route('/hw_proxy/scale_tare/', type='json', auth='none', cors='*')
    def scale_tare(self):
        if scale_thread:
            scale_thread.set_tare()
        return True

    @http.route('/hw_proxy/scale_clear_tare/', type='json', auth='none', cors='*')
    def scale_clear_tare(self):
        if scale_thread:
            scale_thread.clear_tare()
        return True
