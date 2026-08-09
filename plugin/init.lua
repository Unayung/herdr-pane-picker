local wezterm = require("wezterm")

local module = {}
local alphabet = "asdfghjklqwertyuiopzxcvbnm"

local function file_exists(path)
  local file = io.open(path, "rb")
  if file then
    file:close()
    return true
  end
  return false
end

local function installed_helper()
  local separator = package.config:sub(1, 1)
  for _, plugin in ipairs(wezterm.plugin.list()) do
    local helper = plugin.plugin_dir .. separator .. "pane_picker.py"
    local manifest = plugin.plugin_dir .. separator .. "herdr-plugin.toml"
    if file_exists(helper) and file_exists(manifest) then
      return helper
    end
  end
  return nil
end

local function choice_action(python, helper, choice)
  return wezterm.action_callback(function()
    if not helper then
      wezterm.log_error("herdr-pane-picker: could not locate pane_picker.py")
      return
    end
    local command = choice and "choose" or "cancel"
    local args = { python, helper, command }
    if choice then
      table.insert(args, choice)
    end
    wezterm.background_child_process(args)
  end)
end

function module.apply_to_config(config, options)
  options = options or {}
  local python = options.python or "python3"
  local helper = options.helper_path or installed_helper()
  local table_name = options.key_table or "herdr_pane_picker"
  local timeout = options.timeout_milliseconds or 6000
  local is_herdr = options.is_herdr or function()
    return true
  end

  config.key_tables = config.key_tables or {}
  config.key_tables[table_name] = {}
  for index = 1, #alphabet do
    local hint = alphabet:sub(index, index)
    table.insert(config.key_tables[table_name], {
      key = hint,
      mods = "NONE",
      action = choice_action(python, helper, hint),
    })
    -- An uppercase choice asks the helper to zoom. ponytail: bind both the
    -- shifted glyph and the base key, WezTerm reports either one.
    for _, shifted in ipairs({ hint:upper(), hint }) do
      table.insert(config.key_tables[table_name], {
        key = shifted,
        mods = "SHIFT",
        action = choice_action(python, helper, hint:upper()),
      })
    end
  end
  table.insert(config.key_tables[table_name], {
    key = "Escape",
    mods = "NONE",
    action = choice_action(python, helper, nil),
  })
  table.insert(config.key_tables[table_name], {
    key = "g",
    mods = "CTRL",
    action = choice_action(python, helper, nil),
  })

  config.keys = config.keys or {}
  table.insert(config.keys, {
    key = options.key or "p",
    mods = options.mods or "CMD|SHIFT",
    action = wezterm.action_callback(function(window, pane)
      if not is_herdr(window, pane) then
        window:perform_action(options.fallback_action or wezterm.action.PaneSelect, pane)
        return
      end
      pane:send_text(options.trigger_sequence or "\x1b[112;10u")
      window:perform_action(
        wezterm.action.ActivateKeyTable({
          name = table_name,
          one_shot = true,
          timeout_milliseconds = timeout,
          prevent_fallback = true,
        }),
        pane
      )
    end),
  })
end

return module
