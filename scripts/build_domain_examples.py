"""Writes examples/domains/<name>/{faq.csv,heldout.csv}: small FAQs from unrelated fields (and two languages), each with held-out questions
written in other words than the FAQ's own phrasings. They exist to show that nothing in the engine depends on what a FAQ is about.

    python scripts/build_domain_examples.py            # writes the files, checks them, prints a summary

FAQ line:      id | question | answer | alternate question ; another   (alternates optional)
Held-out line: split | type | id | question                           (id empty for out-of-scope questions)

The held-out questions and the FAQs have the same author (not customers), so absolute scores are optimistic; they are for comparing systems and
for finding things that fall apart, not for promising accuracy.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DOMAINS: dict[str, tuple[str, str]] = {}

DOMAINS["clinic"] = ("""
hours | What are your opening hours? | We are open Monday to Friday from 8am to 6pm and Saturday from 9am to 1pm. The clinic is closed on Sundays. | When is the clinic open?; What time do you close?
book | How do I book an appointment? | You can book online, by phone on 555-0100, or at the front desk. New patients should arrive 15 minutes early to fill in forms. | Can I make an appointment?
cancel | How do I cancel or reschedule an appointment? | Please give at least 24 hours' notice by phone or online. Late cancellations may incur a 25 dollar fee.
insurance | Which insurance plans do you accept? | We accept most major plans including Aetna, Cigna and BlueCross. Bring your insurance card to every visit. | Do you take my insurance?
walkin | Do you accept walk-in patients? | Yes, walk-ins are seen on a first come basis, but patients with appointments are seen first so waits can be long.
records | How can I get a copy of my medical records? | Fill in a release form at the front desk or download it from our website. Records are ready within 5 business days.
prescription | How do I get a prescription refilled? | Call your pharmacy and ask them to send us a request. Refills take up to 2 business days. | Can I renew my medication?
covid | Do you offer COVID-19 vaccines? | Yes, vaccines and boosters are available by appointment, at no cost with most insurance plans.
lab | Can I have blood tests done at the clinic? | Yes, our on-site lab does blood draws from 8am to noon. Fasting tests need an appointment before 10am.
children | Do you treat children? | We see patients of all ages, from newborns to seniors. Paediatric visits are on Tuesday and Thursday afternoons.
parking | Is there parking at the clinic? | Free parking is available behind the building, with accessible spaces by the main entrance.
telehealth | Do you offer video visits? | Yes, telehealth visits are available for follow-ups and minor illnesses. You will receive a link by email 10 minutes before the visit.
""", """
dev | paraphrase | hours | when do you shut on saturdays
dev | paraphrase | book | i want to see a doctor next week how do i set that up
dev | paraphrase | cancel | can i move my visit to another day
dev | paraphrase | insurance | is aetna ok with you
dev | paraphrase | walkin | can i just turn up without booking
dev | paraphrase | records | need my chart sent to a new doctor
dev | paraphrase | prescription | my pills are running out
dev | paraphrase | covid | where can i get a covid jab
dev | paraphrase | lab | do i need to fast before the blood work
dev | paraphrase | children | are kids welcome
dev | out-of-scope-near | | how much does a knee operation cost
dev | out-of-scope-far | | what is the capital of canada
test | paraphrase | hours | are you open on sunday
test | short | hours | clinic hours
test | paraphrase | book | set up a first visit as a new patient
test | typo | book | appointmnt booking
test | answer-terms | cancel | late cancel fee 25 dollars
test | paraphrase | insurance | do you work with cigna
test | short | insurance | insurance cards
test | paraphrase | walkin | how long is the wait for walk ins
test | answer-terms | records | release form for my records
test | paraphrase | prescription | how quickly are refills done
test | short | covid | booster shot availability
test | answer-terms | lab | fasting blood test time
test | short | children | kids paediatric days
test | paraphrase | parking | where do i park
test | typo | parking | parkng
test | paraphrase | telehealth | can i see the doctor over video
test | answer-terms | telehealth | telehealth link by email
test | out-of-scope-near | | do you do cosmetic surgery
test | out-of-scope-near | | what are the side effects of ibuprofen
test | out-of-scope-far | | who won the cricket match yesterday
test | out-of-scope-far | | recommend a good pasta recipe
""")

DOMAINS["saas"] = ("""
reset | How do I reset my password? | Click "Forgot password" on the sign-in page and we will email you a reset link that is valid for 30 minutes. | I forgot my password
invite | How do I invite teammates? | Open Settings, choose Members, and click Invite. Teammates get an email with a join link. | Can I add people to my workspace?
pricing | How much does Taskly cost? | The Free plan is 0 dollars for up to 5 users. Team is 8 dollars per user per month and Business is 15 dollars per user per month. | What are your plans?
cancel | How do I cancel my subscription? | Go to Settings, Billing, and choose Cancel plan. You keep access until the end of the billing period.
export | Can I export my data? | Yes. Use Settings, Data, Export to download all projects as CSV or JSON.
integrations | Which integrations do you support? | Taskly integrates with Slack, Google Calendar, GitHub and Zapier.
sso | Do you support single sign-on? | SSO with Google, Okta and Microsoft is available on the Business plan.
api | Is there an API? | Yes, the REST API is available on the Team and Business plans. Create a token under Settings, Developer.
mobile | Is there a mobile app? | Taskly has apps for iOS and Android, available from the App Store and Google Play.
security | Is my data secure? | All data is encrypted in transit and at rest, and we are SOC 2 Type II certified. Backups run every night.
support | How do I contact support? | Email help@taskly.example or use the chat bubble in the app. Business customers get a 4 hour response time.
refund | Do you offer refunds? | We refund annual plans within 14 days of purchase. Monthly plans are not refundable.
""", """
dev | paraphrase | reset | cant log in i forgot my passcode
dev | paraphrase | invite | how can i get my coworkers into the workspace
dev | paraphrase | pricing | what do the paid tiers charge
dev | paraphrase | cancel | stop my plan
dev | paraphrase | export | download everything i have stored
dev | paraphrase | integrations | does it work with slack
dev | paraphrase | sso | can we log in with okta
dev | paraphrase | api | do you expose an endpoint for developers
dev | paraphrase | mobile | can i use it on my phone
dev | paraphrase | security | how safe is my information
dev | out-of-scope-near | | how do i change my profile picture
dev | out-of-scope-far | | how many moons does jupiter have
test | answer-terms | reset | reset link expiry thirty minutes
test | paraphrase | pricing | team plan price per user
test | paraphrase | pricing | how many people can use the free plan
test | paraphrase | refund | annual refund window
test | short | refund | monthly refunds
test | short | mobile | ios android app
test | short | security | soc2
test | answer-terms | security | nightly backups
test | paraphrase | support | how fast does business support reply
test | answer-terms | export | export csv json
test | short | integrations | zapier github
test | answer-terms | invite | invite email join link
test | paraphrase | cancel | do i keep access after cancelling
test | answer-terms | api | rest api token
test | out-of-scope-near | | how do i add a gantt chart
test | out-of-scope-near | | can taskly track my working hours
test | out-of-scope-far | | what is the best laptop for gaming
test | out-of-scope-far | | translate hello into german
""")

DOMAINS["restaurant"] = ("""
hours | What are your opening hours? | Lunch is 12 to 3pm and dinner is 6 to 10pm, Tuesday to Sunday. We are closed on Mondays.
reserve | How do I make a reservation? | Reserve online or call 555-0142. Parties of 8 or more need to book at least 3 days ahead. | Can I book a table?
vegetarian | Do you have vegetarian options? | Yes, over a third of the menu is vegetarian, and we can make most pasta dishes vegan on request.
glutenfree | Do you offer gluten-free dishes? | We have gluten-free pasta and pizza bases for a 2 dollar supplement. Please tell your server about any allergies.
delivery | Do you deliver? | We deliver within 5 km through our website and the major delivery apps. Delivery is free over 30 dollars.
dress | Is there a dress code? | No dress code. Smart casual is most common.
kids | Do you have a children's menu? | Yes, kids under 12 can choose from a small menu for 9 dollars including a drink.
parking | Where can I park? | There is a public car park 2 minutes away on Elm Street, and street parking is free after 6pm.
private | Can I book the restaurant for a private event? | Yes, our upstairs room seats up to 40 guests. Contact events@luigis.example for packages.
giftcards | Do you sell gift cards? | Gift cards from 25 dollars are available at the restaurant and online.
service | Do you add a service charge? | A 12 percent service charge is added for parties of 6 or more. Otherwise tipping is at your discretion.
dogs | Are dogs allowed? | Dogs are welcome on the outdoor terrace but not inside.
""", """
dev | paraphrase | hours | what time does dinner start
dev | paraphrase | reserve | can i get a table for friday
dev | paraphrase | vegetarian | anything for vegans
dev | paraphrase | glutenfree | i have coeliac disease
dev | paraphrase | delivery | will you bring food to my house
dev | paraphrase | dress | should i wear a suit
dev | paraphrase | kids | menu for little ones
dev | paraphrase | parking | is there anywhere to leave my car
dev | paraphrase | private | can we hire the place for a party
dev | paraphrase | giftcards | voucher as a present
dev | out-of-scope-near | | do you have a wine list
dev | out-of-scope-far | | who is the prime minister of japan
test | paraphrase | hours | are you open on monday
test | short | hours | closed days
test | answer-terms | reserve | party of ten booking notice
test | paraphrase | reserve | table for two tonight
test | short | vegetarian | vegan pasta
test | answer-terms | glutenfree | gluten free supplement price
test | paraphrase | delivery | free delivery minimum order
test | short | dress | smart casual
test | answer-terms | kids | kids under 12 menu price
test | answer-terms | parking | elm street car park
test | paraphrase | private | upstairs room capacity
test | paraphrase | giftcards | gift card minimum amount
test | paraphrase | service | service charge for large groups
test | paraphrase | dogs | can i bring my dog
test | out-of-scope-near | | do you do catering for weddings
test | out-of-scope-near | | how much is the tiramisu
test | out-of-scope-far | | how do i fix a leaking tap
test | out-of-scope-far | | what is 15 times 12
""")

DOMAINS["trivia"] = ("""
france | What is the capital of France? | Paris is the capital of France. | Which city is France's capital?
everest | How tall is Mount Everest? | Mount Everest is 8,849 metres (29,032 feet) tall.
shakespeare | Who wrote Romeo and Juliet? | William Shakespeare wrote Romeo and Juliet around 1595.
lightspeed | What is the speed of light? | Light travels at about 299,792 kilometres per second.
sky | Why is the sky blue? | Air scatters short blue wavelengths of sunlight more than longer red ones, so the sky looks blue.
moon | How far away is the Moon? | The Moon is about 384,400 kilometres from Earth.
monalisa | Who painted the Mona Lisa? | Leonardo da Vinci painted the Mona Lisa in the early 1500s.
phone | Who invented the telephone? | Alexander Graham Bell patented the telephone in 1876.
spider | How many legs does a spider have? | Spiders have eight legs.
universe | How old is the universe? | The universe is about 13.8 billion years old.
fastest | What is the fastest land animal? | The cheetah, which can run at about 100 kilometres per hour.
tallest | What is the tallest building in the world? | The Burj Khalifa in Dubai, at 828 metres.
""", """
dev | paraphrase | france | name the french capital
dev | paraphrase | everest | height of everest
dev | paraphrase | shakespeare | author of romeo and juliet
dev | paraphrase | lightspeed | how fast does light move
dev | paraphrase | sky | reason the sky looks blue
dev | paraphrase | moon | distance to the moon
dev | paraphrase | monalisa | who is the artist behind the mona lisa
dev | paraphrase | phone | who came up with the telephone
dev | paraphrase | spider | number of spider legs
dev | paraphrase | universe | age of the cosmos
dev | out-of-scope-near | | what is the capital of australia
dev | out-of-scope-far | | what is the best pizza topping
test | short | france | france capital city
test | short | everest | mount everest height metres
test | paraphrase | shakespeare | romeo and juliet writer
test | paraphrase | lightspeed | speed of light in km per second
test | paraphrase | sky | why are skies blue
test | short | moon | moon distance km
test | short | monalisa | mona lisa painter
test | short | phone | telephone inventor
test | answer-terms | spider | spiders eight legs
test | paraphrase | universe | how many years old is the universe
test | paraphrase | fastest | quickest animal on land
test | short | tallest | worlds tallest building
test | answer-terms | tallest | burj khalifa height
test | answer-terms | fastest | cheetah speed
test | out-of-scope-near | | who wrote hamlet
test | out-of-scope-near | | how tall is mount kilimanjaro
test | out-of-scope-far | | how do i renew my passport
test | out-of-scope-far | | what does my insurance cover
""")

DOMAINS["hinglish-store"] = ("""
timing | Aapki dukaan kab khulti hai? | Dukaan subah 9 baje se raat 9 baje tak khulti hai, aur ravivaar ko bhi khuli rehti hai. | Dukaan ka time kya hai?
delivery | Kya aap ghar par delivery karte hain? | Haan, 5 km ke andar delivery free hai. 500 rupaye se zyada ke order par delivery hamesha free hai.
payment | Payment kaise kar sakte hain? | Aap UPI, card ya cash on delivery se payment kar sakte hain.
return | Saman wapas kaise karein? | Saman 7 din ke andar wapas ho sakta hai, bas packing aur bill saath hona chahiye.
refund | Refund kitne din mein milega? | Refund 3 se 5 kaam ke dino mein aapke bank account mein aa jata hai.
offer | Koi discount ya offer chal raha hai? | Har mahine ke pehle hafte mein 10 percent ka discount milta hai, aur nayi customers ko pehle order par 50 rupaye off.
tracking | Mera order kahan tak pahuncha, kaise pata karein? | Aapko SMS mein tracking link milega, ya aap hume 98765 43210 par call kar sakte hain.
wrongitem | Galat saman aa gaya to kya karein? | Turant hume photo ke saath WhatsApp karein, hum 24 ghante mein sahi saman bhej denge.
bulk | Kya bulk order par alag rate milta hai? | Haan, 10 se zyada items ke order par special rate milta hai. Aap hume call karke puchh sakte hain.
address | Delivery ka address kaise badlein? | Order dispatch hone se pehle hume call ya WhatsApp karke address badal sakte hain.
fresh | Kya saman fresh hota hai? | Sabzi aur dairy roz subah taaza aati hai, aur expiry date pack par likhi hoti hai.
membership | Kya koi membership card hai? | Haan, 199 rupaye mein saal bhar ka membership milta hai, jisme har order par 5 percent cashback milta hai.
""", """
dev | paraphrase | timing | dukaan sunday ko khuli hai kya
dev | paraphrase | delivery | ghar tak saman mangwa sakte hain kya
dev | paraphrase | payment | upi se paise de sakta hoon kya
dev | paraphrase | return | kharab saman lauta sakte hain
dev | paraphrase | refund | paise wapas kab aayenge
dev | paraphrase | offer | koi sale ya chhoot hai abhi
dev | paraphrase | tracking | order ka status kaise dekhun
dev | paraphrase | wrongitem | ulta saman bhej diya hai
dev | paraphrase | bulk | thok mein kharidne par kya rate hai
dev | paraphrase | address | pata galat daal diya
dev | out-of-scope-near | | kya aap online classes chalate hain
dev | out-of-scope-far | | aaj mausam kaisa rahega
test | short | timing | kitne baje band hoti hai
test | answer-terms | delivery | free delivery 500 rupaye order
test | short | payment | cod available hai
test | answer-terms | return | 7 din return bill packing
test | answer-terms | refund | refund 3 se 5 din
test | paraphrase | offer | pehle order par kuch discount milega
test | answer-terms | tracking | tracking link sms
test | answer-terms | wrongitem | galat item photo whatsapp
test | answer-terms | bulk | 10 items special rate
test | paraphrase | address | dispatch se pehle address change
test | paraphrase | fresh | sabzi taaza hoti hai kya
test | short | fresh | expiry date
test | answer-terms | membership | membership cashback 5 percent
test | short | membership | 199 wala card
test | out-of-scope-near | | kya aap paneer ghar par banate hain
test | out-of-scope-near | | job vacancy hai kya
test | out-of-scope-far | | cricket score kya hai
test | out-of-scope-far | | pm kaun hai
""")

DOMAINS["spanish-hotel"] = ("""
checkin | ¿A qué hora es el check-in? | El check-in es a partir de las 15:00 y el check-out hasta las 12:00. | ¿Cuándo puedo entrar a la habitación?
breakfast | ¿El desayuno está incluido? | Sí, el desayuno buffet está incluido y se sirve de 7:00 a 10:30.
wifi | ¿Hay wifi gratis? | Sí, el wifi es gratuito en todo el hotel.
parking | ¿Tienen aparcamiento? | Tenemos parking privado por 12 euros al día, sujeto a disponibilidad.
pets | ¿Se admiten mascotas? | Aceptamos perros pequeños por 15 euros por noche. Avise al reservar.
cancel | ¿Puedo cancelar mi reserva? | La cancelación es gratuita hasta 48 horas antes de la llegada. Después se cobra la primera noche.
airport | ¿Cómo llego desde el aeropuerto? | El hotel está a 20 minutos del aeropuerto. Ofrecemos traslado privado por 35 euros con reserva previa.
pool | ¿Tienen piscina? | Sí, piscina exterior abierta de junio a septiembre de 9:00 a 20:00.
gym | ¿Hay gimnasio? | El gimnasio está abierto las 24 horas para los huéspedes.
latecheckout | ¿Se puede hacer el check-out tarde? | El check-out tardío hasta las 14:00 cuesta 20 euros, según disponibilidad.
kids | ¿Los niños pagan? | Los niños menores de 6 años se alojan gratis compartiendo habitación con sus padres.
luggage | ¿Pueden guardar mi equipaje? | Sí, guardamos el equipaje gratis antes del check-in y después del check-out.
""", """
dev | paraphrase | checkin | a que hora puedo ocupar la habitacion
dev | paraphrase | breakfast | incluye el desayuno la tarifa
dev | paraphrase | wifi | hay internet en las habitaciones
dev | paraphrase | parking | donde dejo el coche
dev | paraphrase | pets | puedo viajar con mi perro
dev | paraphrase | cancel | si no puedo ir me devuelven el dinero
dev | paraphrase | airport | como se va del aeropuerto al hotel
dev | paraphrase | pool | se puede nadar en verano
dev | paraphrase | gym | puedo entrenar de madrugada
dev | paraphrase | latecheckout | quiero quedarme hasta mas tarde el ultimo dia
dev | out-of-scope-near | | cuanto cuesta una habitacion doble
dev | out-of-scope-far | | cual es la capital de italia
test | short | checkin | hora de entrada
test | short | checkin | hora de salida
test | answer-terms | breakfast | desayuno buffet de 7 a 10:30
test | paraphrase | wifi | la conexion a internet es gratuita
test | answer-terms | parking | parking privado 12 euros
test | answer-terms | pets | perros pequeños 15 euros
test | paraphrase | cancel | hasta cuando puedo cancelar sin pagar
test | answer-terms | airport | traslado privado 35 euros
test | paraphrase | pool | horario de la piscina
test | short | gym | gimnasio 24 horas
test | answer-terms | latecheckout | salida tardia hasta las 14:00
test | paraphrase | kids | mi hijo tiene cinco años tiene que pagar
test | paraphrase | luggage | puedo dejar la maleta antes de entrar
test | short | luggage | consigna de equipaje
test | out-of-scope-near | | tienen servicio de lavanderia
test | out-of-scope-near | | se puede reservar una mesa en el restaurante
test | out-of-scope-far | | quien gano el partido de futbol
test | out-of-scope-far | | como se hace una tortilla
""")


def write(name: str, faq: str, held: str) -> tuple[int, int]:
    out = ROOT / "examples" / "domains" / name
    out.mkdir(parents=True, exist_ok=True)
    with (out / "faq.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "question", "answer", "alt_questions"])
        for line in filter(None, (x.strip() for x in faq.strip().splitlines())):
            parts = [p.strip() for p in line.split(" | ")]
            alts = "|".join(a.strip() for a in parts[3].split(";")) if len(parts) > 3 else ""
            w.writerow([parts[0], parts[1], parts[2], alts])
    with (out / "heldout.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["query", "entry_id", "type", "split"])
        rows = [[p.strip() for p in x.split("|", 3)] for x in held.strip().splitlines() if x.strip()]
        seen: dict[str, int] = {}
        for split, kind, eid, query in rows:
            if not kind.startswith("out-of-scope"):  # dev must look like test (the same mix of kinds of question), or a line chosen on dev will not carry over
                split = "dev" if seen.get(kind, 0) % 2 == 0 else "test"
                seen[kind] = seen.get(kind, 0) + 1
            w.writerow([query, eid, kind, split])
    return len(faq.strip().splitlines()), len(rows)


def main() -> None:
    from faqengine.heldout import load_heldout
    from faqengine.kb import load_kb

    for name, (faq, held) in DOMAINS.items():
        entries, questions = write(name, faq, held)
        kb = load_kb(ROOT / "examples" / "domains" / name / "faq.csv")
        ho = load_heldout(ROOT / "examples" / "domains" / name / "heldout.csv", kb)  # raises on an unknown id or type
        c = ho.counts()
        leaked = [r["query"] for r in ho.removed]
        in_dev = sum(d["dev"] for t, d in c["by_type"].items() if not t.startswith("out-of-scope"))
        print(f"{name:14s} {entries:2d} entries  {questions:2d} held-out ({c['by_split']['dev']} dev / {c['by_split']['test']} test, {in_dev} in-scope dev)  leaked: {leaked or 'none'}")
        assert not leaked, f"{name}: held-out questions that are near-duplicates of a FAQ phrasing: {leaked}"
        assert in_dev >= 10, f"{name}: needs at least 10 in-scope dev questions for training to be judged"


if __name__ == "__main__":
    main()
