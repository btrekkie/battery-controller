# Description
I've read that it's bad to keep a laptop always plugged into a power outlet.
This can cause the battery to swell, which can deform the laptop and make the
trackpad difficult to use. It's preferable to keep it charged at between 40% and
80%. ``battery_control.py`` is a slightly modified version of a Python 3 script
I use to achieve this automatically, without incessantly unplugging and
replugging the laptop by hand.

I have my laptop plugged into a smart plug, which is plugged into a power
outlet. The smart plug can be accessed programatically. This enables my script
to control whether the laptop is charging.

I run ``python3 battery_control.py poll`` every five minutes. This causes the
battery to repeatedly charge until it reaches 75%, and then discharge until it
reaches 50%. But there's a bit more to the script that this; see the comments
for details.
