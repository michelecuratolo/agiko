"""Test of the full diplomatic path: proposal, pact, deposit, tribute,
betrayal, war pact, separate peace, mutual defence."""
import config as C
from world import World
from orders import apply_orders
import observation as OBS

w = World(["red", "blue", "green"], seed=3)

def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    assert cond, msg

# alliance proposal
e = apply_orders(w, "red", [{"type": "propose", "to": "blue", "treaty": "alliance",
                             "duration": 300, "text": "let us join forces against green"}])
check(not e, f"proposal accepted by the validator ({e})")
pid = list(w.proposals)[0]

obs_blue = OBS.build_observation(w, "blue")
check(obs_blue["incoming_proposals"][0]["from"] == "red", "blue sees the proposal")

gold_before = {f: w.factions[f].resources["gold"] for f in ("red", "blue")}
apply_orders(w, "blue", [{"type": "respond", "id": pid, "accept": True}])
check(w.rel("red", "blue") == "alliance", "relation = alliance")
dep = C.TREATY_DEPOSIT["alliance"]["gold"]
check(all(w.factions[f].resources["gold"] == gold_before[f] - dep for f in ("red", "blue")),
      f"both sides put down the {dep}-chip deposit")

# shared vision between allies
red_vis = w.visible_tiles("red")
blue_tc = [b for b in w.buildings.values() if b.faction == "blue"][0]
check((blue_tc.x, blue_tc.y) in red_vis, "allies share vision")

# tribute
before = w.factions["blue"].resources["food"]
apply_orders(w, "red", [{"type": "tribute", "to": "blue", "resources": {"food": 50}}])
check(w.factions["blue"].resources["food"] == before + 50, "tribute transferred")
e = apply_orders(w, "red", [{"type": "tribute", "to": "blue", "resources": {"gold": 99999}}])
check(e, f"impossible tribute rejected ({e[0][:50]})")

# attacking an ally requires explicit confirmation
ru = [u for u in w.units.values() if u.faction == "red"][0]
bu = [u for u in w.units.values() if u.faction == "blue"][0]
e = apply_orders(w, "red", [{"type": "attack", "unit": ru.id, "target": bu.id}])
check(e and "confirm_betrayal" in e[0], "attack on ally blocked without confirmation")

# betrayal
blue_gold = w.factions["blue"].resources["gold"]
e = apply_orders(w, "red", [{"type": "attack", "unit": ru.id, "target": bu.id,
                             "confirm_betrayal": True}])
check(not e and w.rel("red", "blue") == "war", "betrayal carried out, now at war")
check(w.factions["blue"].resources["gold"] == blue_gold + 2 * dep,
      "the victim gets its deposit back plus the betrayer's")
check(w.factions["red"].betrayals == 1, "betrayal recorded")
check(any("BETRAYAL" in l for l in w.public_log), "betrayal announced publicly")
check(OBS.build_observation(w, "green")["betrayals_by_faction"]["red"] == 1,
      "green sees red's reputation")

# readable errors
e = apply_orders(w, "red", [{"type": "teleport", "unit": ru.id},
                            {"type": "move", "unit": "u999", "to": [1, 1]},
                            {"type": "move", "unit": ru.id, "to": [999, 999]},
                            {"type": "gather", "unit": ru.id, "at": [0, 0]}])
check(len(e) == 4, f"every invalid order rejected with a reason:\n      " +
      "\n      ".join(x[:70] for x in e))

# pact expiry
w2 = World(["a", "b"], seed=1)
apply_orders(w2, "a", [{"type": "propose", "to": "b", "treaty": "truce", "duration": 60}])
apply_orders(w2, "b", [{"type": "respond", "id": list(w2.proposals)[0], "accept": True}])
check(w2.rel("a", "b") == "truce", "truce signed")
for _ in range(62):
    w2.step()
check(w2.rel("a", "b") == "neutral", "the truce expires on its own")

# deposits come back when a pact simply expires
w3 = World(["a", "b"], seed=1)
g0 = w3.factions["a"].resources["gold"]
apply_orders(w3, "a", [{"type": "propose", "to": "b", "treaty": "alliance", "duration": 60}])
apply_orders(w3, "b", [{"type": "respond", "id": list(w3.proposals)[0], "accept": True}])
for _ in range(62):
    w3.step()
check(w3.rel("a", "b") == "neutral" and w3.factions["a"].resources["gold"] >= g0,
      "an expired alliance returns the deposit")

# war pact: both go to war with the common enemy and are allied with each other
w4 = World(["red", "blue", "green"], seed=2)
e = apply_orders(w4, "red", [{"type": "propose", "to": "blue", "treaty": "war_pact",
                              "duration": 200}])
check(e and "against" in e[0], "a war pact without a target is rejected")
apply_orders(w4, "red", [{"type": "propose", "to": "blue", "treaty": "war_pact",
                          "against": "green", "duration": 200}])
apply_orders(w4, "blue", [{"type": "respond", "id": list(w4.proposals)[0], "accept": True}])
check(w4.rel("red", "green") == "war" and w4.rel("blue", "green") == "war"
      and w4.rel("red", "blue") == "alliance", "war pact: both at war with green, allied together")
check(any("war pact against green" in l for l in w4.public_log), "war pact announced")

# mutual defence: blue's turret fires at green even though blue attacks no one
check(w4.defends_against("blue", "green") and not w4.defends_against("blue", "red"),
      "allies treat each other's enemies as their own")
w5 = World(["red", "blue", "green"], seed=2)
apply_orders(w5, "red", [{"type": "propose", "to": "blue", "treaty": "alliance", "duration": 200}])
apply_orders(w5, "blue", [{"type": "respond", "id": list(w5.proposals)[0], "accept": True}])
w5.declare_war("green", "red")
check(w5.rel("blue", "green") == "neutral" and w5.defends_against("blue", "green"),
      "green attacks red: blue's defences engage without blue declaring war")

# separate peace with the common enemy betrays the war-pact partner
b0 = w4.factions["blue"].betrayals
apply_orders(w4, "blue", [{"type": "propose", "to": "green", "treaty": "truce", "duration": 100}])
apply_orders(w4, "green", [{"type": "respond", "id": list(w4.proposals)[0], "accept": True}])
check(w4.factions["blue"].betrayals == b0 + 1 and w4.rel("blue", "green") == "truce",
      "separate peace with the enemy counts as betraying the partner")
check(any("separate peace" in l for l in w4.public_log), "separate peace announced")

# damage to a construction site must stick while drones keep building
w6 = World(["red", "blue"], seed=3)
hub6 = [b for b in w6.buildings.values() if b.faction == "red"][0]
spot = next((x, y) for y in range(48) for x in range(48)
            if w6.footprint_free("ai_center", x, y) and abs(x - hub6.x) + abs(y - hub6.y) < 12)
site = w6.spawn_building("red", "ai_center", *spot)
for _ in range(6):
    wu = w6.spawn_unit("red", "worker", spot[0] - 1, spot[1])
    wu.order = {"type": "build", "bid": site.id}
for _ in range(40):
    w6.step()
hp0 = site.hp
w6._damage(site, 150, "blue")
w6.step()
check(site.hp < hp0 - 100, "damage to an AI Center site under construction is not erased")

print("\nAll diplomacy tests passed.")
