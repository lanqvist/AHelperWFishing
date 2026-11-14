-- Independent of the Rogue HUD. Only public UI APIs; never casts or loots.
-- Protocol v2: catch confirmation is tied to a cast serial, game errors carry
-- a serial and a code, and every relevant event is kept in a small log.
local _, ns = ...
local F = {}
ns.Fishing = F
local frame, strip, bar, marker, title, detail, db, exportWindow
local cells = {}
local castSerial, lootSerial, lootCast, beat = 0, 0, 0, 0
local errorSerial, errorCode, errorText, errorAt = 0, 0, nil, nil
local activeStart, lastFishingAt, endedCast, endedAt, confirmedCast
local lootOpen, lootFishing, lootForCast
local pendingSlots, pendingCount = {}, 0
local function secret(v) return issecretvalue and issecretvalue(v) end
local function read(fn, ...)
  if type(fn) ~= "function" then return nil end
  local ok, value = pcall(fn, ...)
  if not ok or secret(value) then return nil end
  return value
end
local function plainNumber(v) return not secret(v) and type(v) == "number" and v == v end
local function plainString(v) return not secret(v) and type(v) == "string" end
local function message(s) print("|cff00ffffAHWF Fishing:|r " .. s) end

local LOG_LIMIT = 200
local function log(event, detail)
  if not db then return end
  db.log = type(db.log) == "table" and db.log or {}
  local line = string.format("%.2f %s c=%d l=%d %s", GetTime(), event, castSerial, lootSerial, detail or "")
  table.insert(db.log, line)
  while #db.log > LOG_LIMIT do table.remove(db.log, 1) end
end
F.Log = log

function F.FreeSlots()
  local freeFn = C_Container and C_Container.GetContainerNumFreeSlots or GetContainerNumFreeSlots
  local sizeFn = C_Container and C_Container.GetContainerNumSlots or GetContainerNumSlots
  if type(freeFn) ~= "function" or type(sizeFn) ~= "function" then return nil end
  local total = 0
  for bag = 0, (NUM_BAG_SLOTS or 4) do
    local size = read(sizeFn, bag)
    if not plainNumber(size) then return nil end
    if size > 0 then
      local ok, free, family = pcall(freeFn, bag)
      -- The backpack is always general-purpose; some clients omit its family.
      if ok and not secret(family) and family == nil and bag == 0 then family = 0 end
      if not ok or not plainNumber(free) or not plainNumber(family) then return nil end
      -- Only general-purpose slots: quivers and profession bags cannot hold all catches.
      if family == 0 then total = total + free end
    end
  end
  return math.min(total, 65534)
end

local function fishingName()
  if db.spellName and db.spellName ~= "" then return db.spellName end
  return read(C_Spell and C_Spell.GetSpellName or GetSpellInfo, 7620)
end

local function endActive(reason)
  if activeStart then
    endedCast, endedAt, activeStart = castSerial, GetTime(), nil
    log("FISH_END", reason)
  end
end

function F.CastState()
  local ok, name, _, _, startMS, endMS = pcall(UnitChannelInfo, "player")
  if not ok or secret(name) then return 5, 0 end
  if not name then
    ok, name, _, _, startMS, endMS = pcall(UnitCastingInfo, "player")
    if not ok or secret(name) then return 5, 0 end
  end
  if not name then endActive("none"); return 0, 0 end
  local fish = fishingName()
  if not fish then return 5, 0 end
  if name ~= fish then endActive("other"); return 2, 0 end
  if not plainNumber(startMS) or not plainNumber(endMS) or endMS <= startMS then return 5, 0 end
  if activeStart ~= startMS then
    -- A recast can replace the channel between two polls: close the old cast first.
    endActive("replaced")
    castSerial = (castSerial + 1) % 65536
    activeStart = startMS
    log("FISH_START", string.format("%.1fs", (endMS - startMS) / 1000))
  end
  lastFishingAt = GetTime()
  return 1, math.max(0, math.min(1, (GetTime() * 1000 - startMS) / (endMS - startMS)))
end

function F.InputAllowed()
  local focusFn = GetCurrentKeyBoardFocus or GetCurrentKeyboardFocus
  if type(focusFn) ~= "function" then return nil end
  local ok, focus = pcall(focusFn)
  local dead = read(UnitIsDeadOrGhost, "player")
  local combat = read(UnitAffectingCombat, "player")
  local speed = read(GetUnitSpeed, "player")
  if not ok or secret(focus) or dead == nil or combat == nil or not plainNumber(speed) then return nil end
  return not (focus or dead or combat or speed > 0 or (GameMenuFrame and GameMenuFrame:IsShown()))
end

function F.Packet(state, free, progress)
  free = free or 65535
  local data = {2, state, math.floor(free / 256), free % 256,
    math.floor(progress * 255 + .5), math.floor(castSerial / 256), castSerial % 256,
    lootSerial, lootCast, beat, errorSerial * 16 + errorCode}
  local sum = 0
  for i, v in ipairs(data) do sum = sum + i * v end
  data[12] = sum % 256
  return data
end

-- The cast a catch belongs to: the open fishing loot window, else the cast that
-- ended moments ago, else the still-visible channel (event before our poll).
local function catchCast()
  if lootFishing and lootForCast then return lootForCast end
  if endedCast and endedAt and GetTime() - endedAt < 5 then return endedCast end
  if activeStart and lastFishingAt and GetTime() - lastFishingAt < .5 then return castSerial end
  return nil
end

local function confirm(source)
  local cast = catchCast()
  if not cast then log("CATCH_IGNORED", source .. " no recent fishing"); return end
  if cast == confirmedCast then log("CATCH_DUP", source); return end
  confirmedCast = cast
  lootSerial = (lootSerial + 1) % 256
  lootCast = cast % 256
  log("CATCH", source .. " cast=" .. cast)
end
F.Confirm = confirm

local function pattern(format)
  if not plainString(format) then return nil end
  local p = format:gsub("([%(%)%.%%%+%-%*%?%[%]%^%$])", "%%%1")
  p = p:gsub("%%%%s", "(.+)"):gsub("%%%%d", "(%%d+)")
  return "^" .. p .. "$"
end
local selfLoot
local function isSelfLoot(text)
  if not plainString(text) then return false end
  if not selfLoot then
    selfLoot = {}
    for _, key in ipairs({"LOOT_ITEM_SELF", "LOOT_ITEM_SELF_MULTIPLE", "LOOT_ITEM_PUSHED_SELF", "LOOT_ITEM_PUSHED_SELF_MULTIPLE"}) do
      local p = pattern(_G[key]); if p then table.insert(selfLoot, p) end
    end
  end
  for _, p in ipairs(selfLoot) do if text:match(p) then return true end end
  return false
end
F.IsSelfLoot = isSelfLoot

local function errorKind(text)
  if not plainString(text) then return 1 end
  if text == _G.ERR_FISH_NOT_HOOKED then return 2 end
  if text == _G.ERR_FISH_ESCAPED or text == "Рыбка уплыла!" or text == "Your fish got away!" then return 3 end
  if text == _G.ERR_INV_FULL or text == _G.INVENTORY_FULL then return 4 end
  return 1
end

local function raiseError(text)
  errorSerial = (errorSerial + 1) % 16
  errorCode = errorKind(text)
  errorText, errorAt = plainString(text) and text or "?", GetTime()
  log("ERROR", errorCode .. " " .. errorText)
end

local function paint(data)
  for i, byte in ipairs(data) do
    for bitIndex = 0, 7 do
      local value = math.floor(byte / 2 ^ (7 - bitIndex)) % 2
      cells[(i - 1) * 8 + bitIndex + 1]:SetColorTexture(value, value, value, 1)
    end
  end
end

local stateNames = { [0] = "ГОТОВ", [1] = "РЫБАЛКА", [2] = "ДРУГОЙ КАСТ",
  [3] = "ДОБЫЧА", [4] = "ВВОД ЗАПРЕЩЁН", [5] = "НЕТ ДАННЫХ" }
local function update()
  local state, progress = F.CastState()
  local allowed, free = F.InputAllowed(), F.FreeSlots()
  if lootOpen then state = 3 end
  if allowed == nil then state = 5 elseif not allowed then state = 4 end
  beat = (beat + 1) % 256
  paint(F.Packet(state, free, progress))
  bar:SetValue(progress)
  marker:ClearAllPoints()
  marker:SetPoint("CENTER", bar, "LEFT", bar:GetWidth() * db.mark / 100, 0)
  local recentError = errorAt and GetTime() - errorAt < 6 and errorText
  title:SetText("РЫБАЛКА — " .. (recentError and ("ОШИБКА: " .. recentError) or stateNames[state]))
  detail:SetText(string.format("%.0f%%   Места: %s   Улов подтверждён: %d", progress * 100,
    free and tostring(free) or "?", lootSerial))
end

local function texture(parent, x, y, w, h, r, g, b)
  local t = parent:CreateTexture(nil, "ARTWORK")
  t:SetPoint("TOPLEFT", x, -y); t:SetSize(w, h); t:SetColorTexture(r, g, b, 1)
  return t
end

local function build()
  if frame then return end
  frame = CreateFrame("Frame", "AHelperWFishingHUD", UIParent)
  frame:SetSize(340, 158); frame:SetFrameStrata("HIGH"); frame:SetClampedToScreen(true)
  frame:SetPoint("TOPLEFT", UIParent, "TOPLEFT", db.x or 360, -(db.y or 120))
  frame:SetMovable(true); frame:EnableMouse(true); frame:RegisterForDrag("LeftButton")
  frame:SetScript("OnDragStart", function(self) self:StartMoving() end)
  frame:SetScript("OnDragStop", function(self)
    self:StopMovingOrSizing()
    db.x, db.y = self:GetLeft(), UIParent:GetHeight() - self:GetTop()
    message("HUD перемещён: приложение найдёт его заново при следующем запуске")
  end)
  local bg = frame:CreateTexture(nil, "BACKGROUND"); bg:SetAllPoints(); bg:SetColorTexture(0, 0, 0, 1)
  title = frame:CreateFontString(nil, "OVERLAY", "GameFontNormal")
  title:SetPoint("TOPLEFT", 10, -10); title:SetPoint("RIGHT", -10, 0); title:SetJustifyH("LEFT")
  title:SetText("РЫБАЛКА")
  detail = frame:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
  detail:SetPoint("TOPLEFT", 10, -34)
  bar = CreateFrame("StatusBar", nil, frame); bar:SetPoint("TOPLEFT", 10, -58); bar:SetSize(312, 20)
  bar:SetStatusBarTexture("Interface\\Buttons\\WHITE8X8"); bar:SetStatusBarColor(0, .8, .4, 1); bar:SetMinMaxValues(0, 1)
  local back = bar:CreateTexture(nil, "BACKGROUND"); back:SetAllPoints(); back:SetColorTexture(.12, .12, .12, 1)
  marker = bar:CreateTexture(nil, "OVERLAY"); marker:SetSize(3, 26); marker:SetColorTexture(1, .7, 0, 1)
  bar:EnableMouse(true)
  bar:SetScript("OnMouseDown", function(self)
    local x = GetCursorPosition()
    db.mark = math.max(1, math.min(95, math.floor((x / self:GetEffectiveScale() - self:GetLeft()) / self:GetWidth() * 100 + .5)))
    message("Отметка " .. db.mark .. "% (нужна только для режима шкалы)")
  end)
  strip = CreateFrame("Frame", nil, frame); strip:SetPoint("TOPLEFT", 10, -90); strip:SetSize(312, 48)
  for row = 0, 3 do
    texture(strip, 0, row * 12, 12, 12, 1, 0, 1)
    texture(strip, 25 * 12, row * 12, 12, 12, 0, 1, 1)
    for col = 1, 24 do cells[row * 24 + col] = texture(strip, col * 12, row * 12, 12, 12, 0, 0, 0) end
  end
  local hint = frame:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
  hint:SetPoint("BOTTOMLEFT", 10, 5); hint:SetText("Не закрывай этот HUD — /ahwfish log — события")
  local elapsed = 0
  frame:SetScript("OnUpdate", function(_, dt)
    elapsed = elapsed + dt
    if elapsed < .05 then return end
    elapsed = 0
    local ok = pcall(update)
    if not ok then
      beat = (beat + 1) % 256; paint(F.Packet(5, nil, 0)); title:SetText("РЫБАЛКА — API НЕДОСТУПЕН")
    end
  end)
end

local function export()
  build(); frame:Show(); db.enabled = true
  local w, h = GetPhysicalScreenSize()
  local x, bottom, width, height = strip:GetRect()
  local sx = w / (UIParent:GetWidth() * UIParent:GetEffectiveScale()) * strip:GetEffectiveScale()
  local sy = h / (UIParent:GetHeight() * UIParent:GetEffectiveScale()) * strip:GetEffectiveScale()
  local text = string.format('{"schema":"AHelperWForever.Fishing","version":1,"screen":{"width":%d,"height":%d},"rect":{"x":%d,"y":%d,"width":%d,"height":%d},"mark":%d}',
    w, h, math.floor(x * sx + .5), math.floor(h - (bottom + height) * sy + .5), math.floor(width * sx + .5), math.floor(height * sy + .5), db.mark)
  if not exportWindow then
    exportWindow = CreateFrame("Frame", nil, UIParent, "BasicFrameTemplateWithInset")
    exportWindow:SetSize(620, 180); exportWindow:SetPoint("CENTER"); exportWindow:SetFrameStrata("DIALOG")
    exportWindow.TitleText:SetText("Рыбалка: Ctrl+C → Вставить экспорт в приложении")
    local edit = CreateFrame("EditBox", nil, exportWindow, "InputBoxTemplate")
    edit:SetSize(580, 100); edit:SetPoint("CENTER", 0, -10); edit:SetMultiLine(true)
    edit:SetFontObject("ChatFontNormal"); edit:SetAutoFocus(false)
    edit:SetScript("OnEscapePressed", function() exportWindow:Hide() end)
    exportWindow.edit = edit
    exportWindow:SetScript("OnHide", function() edit:ClearFocus() end)
  end
  exportWindow:Show(); exportWindow.edit:SetText(text); exportWindow.edit:SetFocus(); exportWindow.edit:HighlightText()
end

local function lootCount()
  local count = read(GetNumLootItems)
  return plainNumber(count) and count or nil
end

local function onLootWindow(event, arg)
  local recent = lastFishingAt and GetTime() - lastFishingAt < 3
  local isFish = read(IsFishingLoot)
  local count = lootCount()
  if not lootOpen or event == "LOOT_OPENED" then
    lootFishing = recent and isFish ~= false or false
    lootForCast = lootFishing and (activeStart and castSerial or endedCast) or nil
    if not lootOpen then
      -- -1: no slot evidence (unknown count); only the chat receipt can confirm then.
      pendingSlots, pendingCount = {}, -1
      if lootFishing and count and count > 0 then
        for slot = 1, count do pendingSlots[slot] = true end
        pendingCount = count
      end
    end
  end
  lootOpen = true
  log(event, string.format("auto=%s fish=%s recent=%s items=%s forCast=%s", tostring(arg), tostring(isFish),
    tostring(recent), tostring(count), tostring(lootForCast)))
end

local events = CreateFrame("Frame")
for _, e in ipairs({"PLAYER_LOGIN", "LOOT_READY", "LOOT_OPENED", "LOOT_SLOT_CLEARED", "LOOT_CLOSED",
    "CHAT_MSG_LOOT", "UI_ERROR_MESSAGE", "UI_INFO_MESSAGE", "PLAYER_ENTERING_WORLD",
    "UNIT_SPELLCAST_CHANNEL_START", "UNIT_SPELLCAST_CHANNEL_STOP", "UNIT_SPELLCAST_FAILED",
    "UNIT_SPELLCAST_INTERRUPTED"}) do
  pcall(events.RegisterEvent, events, e)
end
events:SetScript("OnEvent", function(_, event, a1, a2)
  if event == "PLAYER_LOGIN" then
    AHelperWFishingDB = AHelperWFishingDB or {}
    db = AHelperWFishingDB
    db.mark = tonumber(db.mark) or 65
    db.mark = math.max(1, math.min(95, math.floor(db.mark)))
    if type(db.log) ~= "table" then db.log = {} end
    log("LOGIN", "protocol=2")
    if db.enabled then build(); frame:Show() end
    return
  end
  if not db or not db.enabled then return end
  if event == "PLAYER_ENTERING_WORLD" then
    lootOpen, lootFishing, lootForCast, activeStart, lastFishingAt = nil, nil, nil, nil, nil
  elseif event == "UI_ERROR_MESSAGE" then
    -- Modern clients: (messageType, text); some older ones pass the text first.
    raiseError(plainString(a2) and a2 or a1)
  elseif event == "UI_INFO_MESSAGE" then
    -- Forever reports "fish escaped" as an info message, not an error.
    local text = plainString(a2) and a2 or (plainString(a1) and a1 or nil)
    log(event, text or "?")
    if text and errorKind(text) ~= 1 then raiseError(text) end
  elseif event == "LOOT_READY" or event == "LOOT_OPENED" then
    onLootWindow(event, a1)
  elseif event == "LOOT_SLOT_CLEARED" then
    if not secret(a1) and pendingSlots[a1] then pendingSlots[a1] = nil; pendingCount = pendingCount - 1 end
    log(event, tostring(a1) .. " left=" .. pendingCount)
  elseif event == "LOOT_CLOSED" then
    log(event, string.format("fishing=%s left=%d", tostring(lootFishing), pendingCount))
    if lootFishing and pendingCount == 0 then confirm("slots") end
    lootOpen, lootFishing, lootForCast = false, false, nil
  elseif event == "CHAT_MSG_LOOT" then
    local mine = isSelfLoot(a1)
    log(event, "self=" .. tostring(mine) .. " " .. (plainString(a1) and a1:sub(1, 60) or "?"))
    if mine then confirm("chat") end
  elseif a1 == "player" then
    log(event, "")
  end
end)

SLASH_AHWFISH1 = "/ahwfish"
SlashCmdList.AHWFISH = function(text)
  if not db then return end
  local command, value = (text or ""):match("^(%S*)%s*(.-)$")
  if command == "off" then db.enabled = false; if frame then frame:Hide() end; return end
  if command == "reset" then errorText, errorAt = nil, nil; message("Сообщение об ошибке скрыто"); return end
  if command == "log" then
    local n = tonumber(value) or 15
    local entries = type(db.log) == "table" and db.log or {}
    for i = math.max(1, #entries - n + 1), #entries do print(entries[i]) end
    message("Журнал сохраняется в SavedVariables при /reload или выходе")
    return
  end
  if command == "clearlog" then db.log = {}; message("Журнал очищен"); return end
  if command == "name" then db.spellName = value ~= "" and value or nil; message("Название рыбалки: " .. (fishingName() or "?")); return end
  if command == "mark" then
    local n = tonumber(value)
    if not n or n < 1 or n > 95 then message("/ahwfish mark 65 — от 1 до 95%"); return end
    db.mark = math.floor(n)
  end
  if command == "export" then export(); return end
  db.enabled = true; build(); frame:Show()
  message("HUD рыбалки включён. Команды: log, clearlog, export, name <название>, off")
end
