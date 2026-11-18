"""CANDuctor — CAN data collection (Waveshare USB-CAN-A).

A microservice utility in two parts:
  * core   — USB device (CH340) handling over the Waveshare USB-CAN-A
              protocol (byte-compatible with mazda-canbus), HTTP microservice;
  * worker — interactive execution of the test scenario (preparation,
              confirmation, simultaneous HS+MS recording).
"""

__version__ = "0.1.0"