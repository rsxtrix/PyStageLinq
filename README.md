# PyStageLinq
Denon StageLinq implementation in Python, for grabbing track data over the Ethernet "Link" port.


This project was an examination of chrisle's NodeJS StageLinq implementation and instrumentation in Python.
https://github.com/chrisle/StageLinq


Tested on: Denon Prime 4, firmware version 5.0.4

Dependencies: None


This is a script that detects a StageLinq device, generates a valid token, performs a handshake, and subscribes
to the output over the StageLinq protocol. 


It's pretty easy. Just plug an Ethernet cable into your Denon device with a "Link" port, and the other end into a
Raspberry Pi (just about any Pi will do) or an ESP32 (running MicroPython) with an Ethernet RJ-45 port, and run this program using:


python3 stagelinq.py


You will then get output on the CLI showing all available data. 


Basically, I ran the nodeJS code from chrisle, looked at how the connection was being made, and re-implemented it in Python.
I did change it so link local addresses will also work. For me, this is part of a project to implement song-aware
lighting effects through a Raspberry Pi 4 4GB and pass them to controllers using Art-Net over Wi-Fi. I added the
exception for link local addressing because the Prime 4 (in my case) and Pi won't be connected to a routing device
and I'd like to keep the addresses straight.


It appears that the Prime 4 will accept any token as long as it's the right length. It's worth mentioning that I
tried a lot of different tokens and they all worked. The only caveat is: I only have a Prime 4 and can't test
this on any of the other models. 


If you have another Denon product that uses StageLinq and it works, feel free to shoot me a message.


Again, big thanks to the work of chrisle and the other projects credited on their page.
