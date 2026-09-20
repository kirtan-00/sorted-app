"""The fixed vocabulary a discovered category can be named from. Plain nouns and scenes an Indian event,
construction-site, travel or lifestyle shoot could contain, one concept each, lower case, no duplicates.
Each label is scored as "a photo of {label}" (embed.encode_text adds the prefix) against a cluster's
centroid and against every member; the best label names the cluster when the members agree (the gates
in classify.discover). No LLM: a few hundred words and a cosine.

"seated interview" and "small painted house" are plain fallbacks added after the first corporate
documentary: without them six interview clusters could only be "man in a kurta" (then "doctor",
"patient in a hospital", "businessman in a suit" by cascade) and a village of painted huts was "slum"."""

VOCAB: list[str] = [
    # ceremony and ritual
    "havan fire", "priest", "garland", "coconut", "lamp lighting", "ribbon cutting", "puja thali", "rangoli", "idol",
    "temple", "wedding mandap", "bride", "groom", "wedding couple", "flower petals", "marigold flowers", "brass lamp",
    "bhoomi pujan", "groundbreaking shovel", "foundation stone", "inauguration", "trophy", "award ceremony",
    "bouquet", "birthday party", "diwali lights", "holi colours", "dancers on stage", "wedding stage",
    "baraat procession", "horse with rider", "fireworks",
    # construction and site
    "excavator", "bulldozer", "crane", "foundation pit", "brick wall", "scaffolding", "cement mixer", "surveyor",
    "safety helmet", "site plan", "signboard", "construction site", "steel rebar", "concrete pour", "dump truck",
    "jcb backhoe", "road roller", "tower crane", "building under construction", "welding sparks", "worker with tools",
    "construction workers", "drainage pipe", "water tank", "solar panels", "wind turbine", "power lines", "warehouse",
    "factory floor", "machinery", "shipping containers", "tiles", "marble floor", "ladder", "blueprint",
    "architectural model", "site office", "unfinished apartment", "high rise tower", "apartment complex", "villa",
    "bungalow", "farmhouse", "compound wall", "parking lot",
    # people
    "portrait", "group photo", "selfie", "handshake", "speech at a microphone", "crowd", "kids", "baby",
    "elderly man", "elderly woman", "family", "couple", "students", "businessman in a suit", "woman in a sari",
    "man in a kurta", "farmer", "labourer", "police officer", "security guard", "chef", "shopkeeper", "vendor",
    "photographer", "audience seated", "people applauding", "people dancing", "people eating", "person on a phone",
    "person laughing", "person working on a laptop", "panel discussion", "presentation", "team meeting",
    "press conference", "queue of people", "musician", "singer on stage", "cricket players", "football players",
    "cyclist", "yoga", "gym", "graduation gown", "doctor", "patient in a hospital", "seated interview",
    # places
    "beach", "sea", "boat", "harbour", "fort", "palace", "road", "highway", "village street", "market", "farmland",
    "hotel room", "office", "meeting room", "garden", "pool", "rooftop", "drone aerial view", "city skyline",
    "night street", "railway station", "train", "airport", "airplane", "bus stand", "petrol pump", "bridge",
    "flyover", "dam", "lake", "waterfall", "hill station", "forest", "desert", "sand dunes", "rice field",
    "tea plantation", "orchard", "village house", "mud house", "small painted house", "slum", "old city lane", "bazaar", "shopping mall",
    "restaurant", "cafe", "street food stall", "kitchen", "dining table", "living room", "bedroom", "balcony",
    "reception desk", "conference hall", "auditorium", "classroom", "library", "laboratory", "hospital", "clinic",
    "bank", "police station", "mosque", "church", "gurudwara", "ghat on a river", "ruins", "museum", "stadium",
    "cricket ground", "playground", "fountain", "monument", "clock tower", "lighthouse", "fishing boats", "ferry",
    "yacht", "empty room", "window view", "wall texture", "floor tiles",
    # vehicles and objects
    "car", "scooter", "bus", "tractor", "auto rickshaw", "motorcycle", "bicycle", "truck", "ambulance", "food plate",
    "tea cup", "sweets", "flowers", "documents", "laptop", "whiteboard", "drawing", "phone screen", "banner",
    "thali meal", "biryani", "fruit", "vegetables", "spices", "water bottle", "buffet table", "clay pot", "basket",
    "chair", "bed", "table", "bookshelf", "television", "projector screen", "microphone", "speaker", "camera",
    "tripod", "drone", "notebook", "map", "chart", "graph on a screen", "poster", "billboard", "hoarding",
    "shop sign", "money", "jewellery", "sunglasses", "bag", "suitcase", "umbrella", "book", "newspaper", "painting",
    "sculpture", "pottery", "candle", "lantern", "string lights", "flag", "gift box", "wedding invitation",
    "product box", "bottle", "cardboard boxes", "plastic bags", "garbage pile",
    # nature and weather
    "sunset", "sunrise", "clouds", "rain", "dog", "cow", "birds", "palm tree", "mountain", "river", "moon", "stars",
    "fog", "storm", "rainbow", "wet road", "monsoon flood", "snow", "sun flare", "blue sky", "overcast sky", "goat",
    "buffalo", "camel", "elephant", "horse", "monkey", "peacock", "pigeons", "eagle", "seagull", "fish", "butterfly",
    "snake", "tiger", "lion", "banyan tree", "coconut tree", "mango tree", "neem tree", "grass", "flower bed",
    "lotus", "rose", "sunflower", "cactus", "leaves", "rock", "cliff", "waves", "shore", "mangroves", "meadow",
    "valley", "hot air balloon", "smoke", "fire", "bonfire", "campfire", "blurry motion", "screenshot",
]

# Labels that say WHO is in the frame or what occasion it is: the "people" section above plus the
# person and occasion words of the ceremony section. CLIP reads these off clothing and pose, which is
# how men in white shirts on an interview set became "doctor", two men in white kurtas on a beach
# "cricket players", a puja table "vendor" and a rangoli "holi colours". A cluster can only take one
# of these when it is what most members plainly look like (the plain member vote), never because it is
# what sets the cluster apart from the rest of the shoot (see classify.discover).
VOCAB_PEOPLE: frozenset[str] = frozenset(VOCAB[VOCAB.index("portrait"):VOCAB.index("seated interview") + 1]) | frozenset({
    "priest", "lamp lighting", "ribbon cutting", "bride", "groom", "wedding couple", "bhoomi pujan", "inauguration",
    "award ceremony", "birthday party", "holi colours", "dancers on stage", "baraat procession", "horse with rider"})

# Labels that assert a place or condition a plainer label would also cover (a slum is also a village of
# small houses, a screenshot is also whatever is on the screen, a monsoon flood is also the sea). Kept
# because a real shoot can contain them, but they need a decisive member vote (DISCOVER_LOADED_SHARE),
# not the ordinary one: a wrong confident "slum" on a corporate documentary costs more than a missing name.
VOCAB_LOADED: frozenset[str] = frozenset({
    "slum", "monsoon flood", "garbage pile", "screenshot", "hospital", "clinic", "doctor", "patient in a hospital",
    "ambulance", "police station"})
