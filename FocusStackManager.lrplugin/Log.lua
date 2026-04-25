-- Log.lua
-- Singleton logger for the plugin. Import this instead of creating
-- LrLogger instances scattered across files.

local LrLogger = import 'LrLogger'

local logger = LrLogger('FocusStackManager')
logger:enable("logfile")

return logger
