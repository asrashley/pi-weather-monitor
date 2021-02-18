from weather.main import WeatherMonitor, DEVMODE

if __name__ == "__main__":
    wm = WeatherMonitor(DEVMODE)
    wm.run()
