local ns, handlers = {}, {}
local printed = {}
function print(s) table.insert(printed, s) end
function CreateFrame()
  return {RegisterEvent=function() end, SetScript=function(_, key, fn) handlers[key]=fn end}
end
SlashCmdList = {}
LOOT_ITEM_SELF = "Ваша добыча: %s."
LOOT_ITEM_SELF_MULTIPLE = "Ваша добыча: %sx%d."
ERR_FISH_NOT_HOOKED = "Ничего не клюет."
ERR_FISH_ESCAPED = "Рыба сорвалась!"
ERR_INV_FULL = "Инвентарь заполнен."
local now, channel, channelStart, channelEnd = 10, nil, 10000, 30000
function GetTime() return now end
function UnitChannelInfo() return channel, nil, nil, channelStart, channelEnd end
function UnitCastingInfo() return nil end
function GetCurrentKeyBoardFocus() return nil end
function UnitIsDeadOrGhost() return false end
function UnitAffectingCombat() return false end
function GetUnitSpeed() return 0 end
function IsFishingLoot() return true end
local lootItems = 2
function GetNumLootItems() return lootItems end
C_Spell = { GetSpellName=function() return "Fishing" end }
local sizes = {[0]=16,[1]=12,[2]=10,[3]=0,[4]=0}
local families = {[0]=0,[1]=0,[2]=2}
C_Container = {
  GetContainerNumSlots=function(bag) return sizes[bag] end,
  GetContainerNumFreeSlots=function(bag) return 5, families[bag] end,
}
assert(loadfile(arg and arg[1] or "src/ahelperw_fishing/addon/AHelperWFishing/Fishing.lua"))("AHelperWFishing", ns)
handlers.OnEvent(nil, "PLAYER_LOGIN")
local F = ns.Fishing
local function packet() return F.Packet(0, 10, 0) end
local function checksum(p) local s = 0; for i=1,11 do s = s + i * p[i] end; return s % 256 end

assert(F.FreeSlots() == 10, "specialty slots must not count")
families[1] = nil
assert(F.FreeSlots() == nil, "unknown must remain unknown")
families[1] = 0
assert(F.InputAllowed() == true)
GetUnitSpeed = function() return 1 end
assert(F.InputAllowed() == false)
GetUnitSpeed = function() return nil end
assert(F.InputAllowed() == nil)
GetUnitSpeed = function() return 0 end
assert(F.CastState() == 0)
channel = "Fishing"; now = 20
local state, progress = F.CastState()
assert(state == 1 and progress == .5)
local p = F.Packet(state, 10, progress)
assert(p[1] == 2 and p[6] == 0 and p[7] == 1 and p[5] == 128)
assert(checksum(p) == p[12])
F.CastState(); assert(packet()[7] == 1, "same cast must not increment")
channel = "Other"; assert(F.CastState() == 2)
channel = "Fishing"; channelStart = 19000
assert(F.CastState() == 1 and packet()[7] == 2)
channelEnd = nil; assert(F.CastState() == 5)
channelEnd = 30000
AHelperWFishingDB.enabled = true

-- Cast 2 ends, partial loot is not a catch.
channel = nil; F.CastState()
handlers.OnEvent(nil, "LOOT_OPENED", 1)
handlers.OnEvent(nil, "LOOT_SLOT_CLEARED", 1)
handlers.OnEvent(nil, "LOOT_CLOSED")
assert(packet()[8] == 0, "partial loot is not success")
-- All slots cleared + closed confirms once, and the confirmation names cast 2.
channel = "Fishing"; channelStart = 21000; F.CastState() -- cast 3
channel = nil; now = 21; F.CastState()
handlers.OnEvent(nil, "LOOT_READY", 1)
handlers.OnEvent(nil, "LOOT_OPENED", 1)
handlers.OnEvent(nil, "LOOT_SLOT_CLEARED", 1)
handlers.OnEvent(nil, "LOOT_SLOT_CLEARED", 1)
handlers.OnEvent(nil, "LOOT_SLOT_CLEARED", 2)
handlers.OnEvent(nil, "CHAT_MSG_LOOT", "Ваша добыча: [Рыба].")
handlers.OnEvent(nil, "LOOT_CLOSED")
p = packet()
assert(p[8] == 1 and p[9] == 3, "one confirmation, bound to cast 3")

-- Chat receipt alone (no loot window events) confirms the cast that just ended.
now = 22; channel = "Fishing"; channelStart = 22000; F.CastState() -- cast 4
now = 23; channel = nil; F.CastState()
handlers.OnEvent(nil, "CHAT_MSG_LOOT", "Ваша добыча: [Рыба]x2.")
p = packet(); assert(p[8] == 2 and p[9] == 4, "chat receipt confirms cast 4")
-- Someone else's loot or a late message cannot confirm.
handlers.OnEvent(nil, "CHAT_MSG_LOOT", "Петя получает добычу: [Рыба].")
now = 40; handlers.OnEvent(nil, "CHAT_MSG_LOOT", "Ваша добыча: [Рыба].")
assert(packet()[8] == 2, "no fishing nearby: not a catch")

-- Same key recast: new channel replaces the old one before the receipt arrives.
now = 41; channel = "Fishing"; channelStart = 41000; channelEnd = 90000; F.CastState() -- cast 5
now = 42; channelStart = 42000; F.CastState() -- cast 6 replaced 5
handlers.OnEvent(nil, "CHAT_MSG_LOOT", "Ваша добыча: [Рыба].")
p = packet(); assert(p[7] == 6 and p[8] == 3 and p[9] == 5, "receipt belongs to the replaced cast 5")
-- The new cast is not a catch by itself.
now = 43; F.CastState(); assert(packet()[8] == 3)

-- Errors carry serial + code and no longer need a manual reset.
handlers.OnEvent(nil, "UI_ERROR_MESSAGE", 51, "Ничего не клюет.")
p = packet(); assert(p[11] == 1 * 16 + 2, "not hooked")
handlers.OnEvent(nil, "UI_ERROR_MESSAGE", 52, "Рыба сорвалась!")
assert(packet()[11] == 2 * 16 + 3, "escaped")
handlers.OnEvent(nil, "UI_ERROR_MESSAGE", 53, "Инвентарь заполнен.")
assert(packet()[11] == 3 * 16 + 4, "bags full")
handlers.OnEvent(nil, "UI_ERROR_MESSAGE", 54, "Нельзя.")
assert(packet()[11] == 4 * 16 + 1, "other error")
-- Forever: "fish escaped" arrives as UI_INFO_MESSAGE; ordinary info messages are not errors.
handlers.OnEvent(nil, "UI_INFO_MESSAGE", 0, "Рыбка уплыла!")
assert(packet()[11] == 5 * 16 + 3, "escaped via info message")
handlers.OnEvent(nil, "UI_INFO_MESSAGE", 0, "Задание выполнено")
assert(packet()[11] == 5 * 16 + 3, "plain info message ignored")
assert(checksum(packet()) == packet()[12])
assert(#AHelperWFishingDB.log > 10, "events are logged")
SlashCmdList.AHWFISH("log 3")
assert(#printed >= 3)
print = function(s) io.write(s, "\n") end
print("Fishing HUD v2: bags, progress, cast serial, catch per cast, chat receipt, errors, log OK")
