"""The fixed part of the demo world (python -m opplist demo): countries,
places, parties, people and the words the synthetic mails are made of.

Every company and person here is invented; the domains end in .example.
Ports, airports and cities are public geography: real UN/LOCODE and IATA
codes with their coordinates (decimal degrees, rounded). Real carriers keep
their names (ZIM, Korean Air) but their contacts and addresses are invented.
"""

from __future__ import annotations

from . import branding

DOMAIN = branding.INTERNAL_DOMAIN

# ----------------------------------------------------------------- countries
#: (name as the app writes it, iso2, iso3, region)
COUNTRIES = (
    ("USA", "US", "USA", "North America"),
    ("Canada", "CA", "CAN", "North America"),
    ("Mexico", "MX", "MEX", "Latin America"),
    ("Chile", "CL", "CHL", "Latin America"),
    ("Peru", "PE", "PER", "Latin America"),
    ("Brazil", "BR", "BRA", "Latin America"),
    ("Colombia", "CO", "COL", "Latin America"),
    ("Uruguay", "UY", "URY", "Latin America"),
    ("Argentina", "AR", "ARG", "Latin America"),
    ("Ecuador", "EC", "ECU", "Latin America"),
    ("Panama", "PA", "PAN", "Latin America"),
    ("China", "CN", "CHN", "Asia"),
    ("Hong Kong", "HK", "HKG", "Asia"),
    ("South Korea", "KR", "KOR", "Asia"),
    ("Japan", "JP", "JPN", "Asia"),
    ("Taiwan", "TW", "TWN", "Asia"),
    ("Vietnam", "VN", "VNM", "Asia"),
    ("Thailand", "TH", "THA", "Asia"),
    ("Malaysia", "MY", "MYS", "Asia"),
    ("Singapore", "SG", "SGP", "Asia"),
    ("India", "IN", "IND", "Asia"),
    ("Netherlands", "NL", "NLD", "Europe"),
    ("Germany", "DE", "DEU", "Europe"),
    ("Belgium", "BE", "BEL", "Europe"),
    ("Spain", "ES", "ESP", "Europe"),
    ("Italy", "IT", "ITA", "Europe"),
    ("France", "FR", "FRA", "Europe"),
    ("UK", "GB", "GBR", "Europe"),
    ("Unknown", None, None, "Undefined"),
)

# -------------------------------------------------------------------- places
#: (key, name, type, country, subdivision, unlocode, iata, lat, lon, parent key)
#: type is master_location.type: city / airport / seaport. A seaport city
#: (Santos, Callao) is a seaport; an airport points at the city it serves.
PLACES = (
    # USA
    ("CHICAGO, IL", "Chicago", "city", "USA", "IL", "USCHI", None, 41.8781, -87.6298, None),
    ("ORD", "Chicago O'Hare International", "airport", "USA", "IL", None, "ORD", 41.9786, -87.9048, "CHICAGO, IL"),
    ("BOLINGBROOK, IL", "Bolingbrook", "city", "USA", "IL", None, None, 41.6986, -88.0684, None),
    ("MILWAUKEE, WI", "Milwaukee", "city", "USA", "WI", "USMKE", None, 43.0389, -87.9065, None),
    ("DETROIT, MI", "Detroit", "city", "USA", "MI", "USDET", None, 42.3314, -83.0458, None),
    ("INDIANAPOLIS, IN", "Indianapolis", "city", "USA", "IN", "USIND", None, 39.7684, -86.1581, None),
    ("COLUMBUS, OH", "Columbus", "city", "USA", "OH", "USCMH", None, 39.9612, -82.9988, None),
    ("MEMPHIS, TN", "Memphis", "city", "USA", "TN", "USMEM", None, 35.1495, -90.0490, None),
    ("MIAMI, FL", "Miami", "city", "USA", "FL", "USMIA", None, 25.7617, -80.1918, None),
    ("MIA", "Miami International", "airport", "USA", "FL", None, "MIA", 25.7932, -80.2906, "MIAMI, FL"),
    ("NEW YORK, NY", "New York", "city", "USA", "NY", "USNYC", None, 40.7128, -74.0060, None),
    ("JFK", "New York John F. Kennedy International", "airport", "USA", "NY", None, "JFK", 40.6413, -73.7781, "NEW YORK, NY"),
    ("NEWARK, NJ", "Newark", "seaport", "USA", "NJ", "USEWR", None, 40.6840, -74.1502, None),
    ("SAVANNAH, GA", "Savannah", "seaport", "USA", "GA", "USSAV", None, 32.0809, -81.0912, None),
    ("CHARLESTON, SC", "Charleston", "seaport", "USA", "SC", "USCHS", None, 32.7765, -79.9311, None),
    ("NORFOLK, VA", "Norfolk", "seaport", "USA", "VA", "USORF", None, 36.8508, -76.2859, None),
    ("ATLANTA, GA", "Atlanta", "city", "USA", "GA", "USATL", None, 33.7490, -84.3880, None),
    ("ATL", "Atlanta Hartsfield-Jackson International", "airport", "USA", "GA", None, "ATL", 33.6407, -84.4277, "ATLANTA, GA"),
    ("HOUSTON, TX", "Houston", "seaport", "USA", "TX", "USHOU", None, 29.7604, -95.3698, None),
    ("IAH", "Houston George Bush Intercontinental", "airport", "USA", "TX", None, "IAH", 29.9902, -95.3368, "HOUSTON, TX"),
    ("DALLAS, TX", "Dallas", "city", "USA", "TX", "USDAL", None, 32.7767, -96.7970, None),
    ("DFW", "Dallas/Fort Worth International", "airport", "USA", "TX", None, "DFW", 32.8998, -97.0403, "DALLAS, TX"),
    ("LAREDO, TX", "Laredo", "city", "USA", "TX", "USLRD", None, 27.5306, -99.4803, None),
    ("LOS ANGELES, CA", "Los Angeles", "seaport", "USA", "CA", "USLAX", None, 33.7361, -118.2639, None),
    ("LAX", "Los Angeles International", "airport", "USA", "CA", None, "LAX", 33.9416, -118.4085, "LOS ANGELES, CA"),
    ("LONG BEACH, CA", "Long Beach", "seaport", "USA", "CA", "USLGB", None, 33.7542, -118.2165, None),
    ("OAKLAND, CA", "Oakland", "seaport", "USA", "CA", "USOAK", None, 37.7955, -122.2790, None),
    ("SEATTLE, WA", "Seattle", "seaport", "USA", "WA", "USSEA", None, 47.6062, -122.3321, None),
    # Canada
    ("TORONTO, ON", "Toronto", "city", "Canada", "ON", "CATOR", None, 43.6532, -79.3832, None),
    ("YYZ", "Toronto Pearson International", "airport", "Canada", "ON", None, "YYZ", 43.6777, -79.6248, "TORONTO, ON"),
    # Mexico
    ("MEXICO CITY", "Mexico City", "city", "Mexico", None, "MXMEX", None, 19.4326, -99.1332, None),
    ("MEX", "Mexico City Benito Juarez International", "airport", "Mexico", None, None, "MEX", 19.4361, -99.0719, "MEXICO CITY"),
    ("MANZANILLO", "Manzanillo", "seaport", "Mexico", None, "MXZLO", None, 19.0522, -104.3158, None),
    ("VERACRUZ", "Veracruz", "seaport", "Mexico", None, "MXVER", None, 19.1738, -96.1342, None),
    ("LAZARO CARDENAS", "Lázaro Cárdenas", "seaport", "Mexico", None, "MXLZC", None, 17.9567, -102.1944, None),
    ("MONTERREY", "Monterrey", "city", "Mexico", None, "MXMTY", None, 25.6866, -100.3161, None),
    ("GUADALAJARA", "Guadalajara", "city", "Mexico", None, "MXGDL", None, 20.6597, -103.3496, None),
    # Chile
    ("SANTIAGO", "Santiago", "city", "Chile", None, "CLSCL", None, -33.4489, -70.6693, None),
    ("SCL", "Santiago Arturo Merino Benítez International", "airport", "Chile", None, None, "SCL", -33.3930, -70.7858, "SANTIAGO"),
    ("SAN ANTONIO", "San Antonio", "seaport", "Chile", None, "CLSAI", None, -33.5933, -71.6217, None),
    ("VALPARAISO", "Valparaíso", "seaport", "Chile", None, "CLVAP", None, -33.0472, -71.6127, None),
    ("IQUIQUE", "Iquique", "seaport", "Chile", None, "CLIQQ", None, -20.2133, -70.1503, None),
    ("ANTOFAGASTA", "Antofagasta", "seaport", "Chile", None, "CLANF", None, -23.6509, -70.3975, None),
    # Peru
    ("LIMA", "Lima", "city", "Peru", None, "PELIM", None, -12.0464, -77.0428, None),
    ("LIM", "Lima Jorge Chávez International", "airport", "Peru", None, None, "LIM", -12.0219, -77.1143, "LIMA"),
    ("CALLAO", "Callao", "seaport", "Peru", None, "PECLL", None, -12.0566, -77.1181, None),
    ("PAITA", "Paita", "seaport", "Peru", None, "PEPAI", None, -5.0892, -81.1144, None),
    # Brazil
    ("SAO PAULO", "São Paulo", "city", "Brazil", None, "BRSAO", None, -23.5505, -46.6333, None),
    ("GRU", "São Paulo Guarulhos International", "airport", "Brazil", None, None, "GRU", -23.4356, -46.4731, "SAO PAULO"),
    ("VCP", "Campinas Viracopos International", "airport", "Brazil", None, None, "VCP", -23.0074, -47.1345, "SAO PAULO"),
    ("SANTOS", "Santos", "seaport", "Brazil", None, "BRSSZ", None, -23.9608, -46.3336, None),
    ("ITAJAI", "Itajaí", "seaport", "Brazil", None, "BRITJ", None, -26.9078, -48.6619, None),
    ("PARANAGUA", "Paranaguá", "seaport", "Brazil", None, "BRPNG", None, -25.5163, -48.5226, None),
    ("RIO DE JANEIRO", "Rio de Janeiro", "seaport", "Brazil", None, "BRRIO", None, -22.9068, -43.1729, None),
    ("MANAUS", "Manaus", "city", "Brazil", None, "BRMAO", None, -3.1190, -60.0217, None),
    # Colombia
    ("BOGOTA", "Bogotá", "city", "Colombia", None, "COBOG", None, 4.7110, -74.0721, None),
    ("BOG", "Bogotá El Dorado International", "airport", "Colombia", None, None, "BOG", 4.7016, -74.1469, "BOGOTA"),
    ("CARTAGENA", "Cartagena", "seaport", "Colombia", None, "COCTG", None, 10.3910, -75.4794, None),
    ("BUENAVENTURA", "Buenaventura", "seaport", "Colombia", None, "COBUN", None, 3.8801, -77.0312, None),
    ("BARRANQUILLA", "Barranquilla", "seaport", "Colombia", None, "COBAQ", None, 10.9685, -74.7813, None),
    ("MEDELLIN", "Medellín", "city", "Colombia", None, "COMDE", None, 6.2442, -75.5812, None),
    # Uruguay and neighbours
    ("MONTEVIDEO", "Montevideo", "seaport", "Uruguay", None, "UYMVD", None, -34.9011, -56.1645, None),
    ("MVD", "Montevideo Carrasco International", "airport", "Uruguay", None, None, "MVD", -34.8384, -56.0308, "MONTEVIDEO"),
    ("BUENOS AIRES", "Buenos Aires", "seaport", "Argentina", None, "ARBUE", None, -34.6037, -58.3816, None),
    ("GUAYAQUIL", "Guayaquil", "seaport", "Ecuador", None, "ECGYE", None, -2.1709, -79.9224, None),
    ("BALBOA", "Balboa", "seaport", "Panama", None, "PABLB", None, 8.9500, -79.5667, None),
    # Asia
    ("SHANGHAI", "Shanghai", "seaport", "China", None, "CNSHA", None, 31.2304, 121.4737, None),
    ("PVG", "Shanghai Pudong International", "airport", "China", None, None, "PVG", 31.1443, 121.8083, "SHANGHAI"),
    ("NINGBO", "Ningbo", "seaport", "China", None, "CNNGB", None, 29.8683, 121.5440, None),
    ("SHENZHEN", "Shenzhen", "city", "China", None, "CNSZX", None, 22.5431, 114.0579, None),
    ("YANTIAN", "Yantian", "seaport", "China", None, "CNYTN", None, 22.5579, 114.2361, "SHENZHEN"),
    ("QINGDAO", "Qingdao", "seaport", "China", None, "CNTAO", None, 36.0671, 120.3826, None),
    ("XIAMEN", "Xiamen", "seaport", "China", None, "CNXMN", None, 24.4798, 118.0894, None),
    ("HONG KONG", "Hong Kong", "seaport", "Hong Kong", None, "HKHKG", None, 22.3193, 114.1694, None),
    ("HKG", "Hong Kong International", "airport", "Hong Kong", None, None, "HKG", 22.3080, 113.9185, "HONG KONG"),
    ("BUSAN", "Busan", "seaport", "South Korea", None, "KRPUS", None, 35.1796, 129.0756, None),
    ("ICN", "Seoul Incheon International", "airport", "South Korea", None, None, "ICN", 37.4602, 126.4407, None),
    ("TOKYO", "Tokyo", "seaport", "Japan", None, "JPTYO", None, 35.6762, 139.6503, None),
    ("KAOHSIUNG", "Kaohsiung", "seaport", "Taiwan", None, "TWKHH", None, 22.6273, 120.3014, None),
    ("HO CHI MINH CITY", "Ho Chi Minh City", "seaport", "Vietnam", None, "VNSGN", None, 10.8231, 106.6297, None),
    ("SGN", "Ho Chi Minh City Tan Son Nhat International", "airport", "Vietnam", None, None, "SGN", 10.8188, 106.6519,
     "HO CHI MINH CITY"),
    ("LAEM CHABANG", "Laem Chabang", "seaport", "Thailand", None, "THLCH", None, 13.0833, 100.8833, None),
    ("PORT KLANG", "Port Klang", "seaport", "Malaysia", None, "MYPKG", None, 3.0000, 101.4000, None),
    ("SINGAPORE", "Singapore", "seaport", "Singapore", None, "SGSIN", None, 1.2644, 103.8200, None),
    ("BANGALORE", "Bengaluru", "city", "India", None, "INBLR", None, 12.9716, 77.5946, None),
    ("BLR", "Bengaluru Kempegowda International", "airport", "India", None, None, "BLR", 13.1986, 77.7066, "BANGALORE"),
    ("NHAVA SHEVA", "Nhava Sheva", "seaport", "India", None, "INNSA", None, 18.9490, 72.9510, None),
    ("CHENNAI", "Chennai", "seaport", "India", None, "INMAA", None, 13.0827, 80.2707, None),
    # Europe
    ("ROTTERDAM", "Rotterdam", "seaport", "Netherlands", None, "NLRTM", None, 51.9244, 4.4777, None),
    ("HAMBURG", "Hamburg", "seaport", "Germany", None, "DEHAM", None, 53.5511, 9.9937, None),
    ("FRA", "Frankfurt am Main", "airport", "Germany", None, None, "FRA", 50.0379, 8.5622, None),
    ("ANTWERP", "Antwerp", "seaport", "Belgium", None, "BEANR", None, 51.2194, 4.4025, None),
    ("VALENCIA", "Valencia", "seaport", "Spain", None, "ESVLC", None, 39.4699, -0.3763, None),
    ("MADRID", "Madrid", "city", "Spain", None, "ESMAD", None, 40.4168, -3.7038, None),
    ("GENOA", "Genoa", "seaport", "Italy", None, "ITGOA", None, 44.4056, 8.9463, None),
    ("LE HAVRE", "Le Havre", "seaport", "France", None, "FRLEH", None, 49.4944, 0.1079, None),
    ("FELIXSTOWE", "Felixstowe", "seaport", "UK", None, "GBFXT", None, 51.9617, 1.3513, None),
)

#: Other spellings a mail uses: (alias, place key, kind).
ALIASES = (
    ("CHI", "CHICAGO, IL", "code"), ("CHICAGO", "CHICAGO, IL", "legacy_key"),
    ("MIAMI", "MIAMI, FL", "legacy_key"), ("HOUSTON", "HOUSTON, TX", "legacy_key"),
    ("SAVANNAH", "SAVANNAH, GA", "legacy_key"), ("SAV", "SAVANNAH, GA", "code"),
    ("LOS ANGELES", "LOS ANGELES, CA", "legacy_key"), ("DALLAS", "DALLAS, TX", "legacy_key"),
    ("NEW YORK", "NEW YORK, NY", "legacy_key"), ("TORONTO", "TORONTO, ON", "legacy_key"),
    ("SAI", "SAN ANTONIO", "legacy_key"), ("SANTIAGO DE CHILE", "SANTIAGO", "manual"),
    ("VALPARAÍSO", "VALPARAISO", "manual"), ("SÃO PAULO", "SAO PAULO", "manual"),
    ("SSZ", "SANTOS", "code"), ("ITAJAÍ", "ITAJAI", "manual"), ("PARANAGUÁ", "PARANAGUA", "manual"),
    ("BOGOTÁ", "BOGOTA", "manual"), ("CTG", "CARTAGENA", "code"), ("MEDELLÍN", "MEDELLIN", "manual"),
    ("CIUDAD DE MEXICO", "MEXICO CITY", "manual"), ("CDMX", "MEXICO CITY", "manual"),
    ("ZLO", "MANZANILLO", "code"), ("LÁZARO CÁRDENAS", "LAZARO CARDENAS", "manual"),
    ("SHA", "SHANGHAI", "iata_city"), ("NGB", "NINGBO", "code"), ("SZX", "SHENZHEN", "iata_city"),
    ("YTN", "YANTIAN", "code"), ("SAIGON", "HO CHI MINH CITY", "manual"), ("HCMC", "HO CHI MINH CITY", "manual"),
    ("BENGALURU", "BANGALORE", "manual"), ("NSA", "NHAVA SHEVA", "code"), ("RTM", "ROTTERDAM", "code"),
    ("HAM", "HAMBURG", "code"), ("INCHEON", "ICN", "manual"), ("PUS", "BUSAN", "code"),
)

#: Market areas (master_area) and the places inside them.
AREAS = (
    ("SANTIAGO", "Santiago metropolitan area", "CL", ("SANTIAGO", "SCL", "SAN ANTONIO", "VALPARAISO")),
    ("SAO PAULO", "São Paulo and Santos", "BR", ("SAO PAULO", "GRU", "VCP", "SANTOS")),
    ("CHICAGO", "Chicagoland", "US", ("CHICAGO, IL", "ORD", "BOLINGBROOK, IL")),
)

# ------------------------------------------------------------------- parties
#: (key, full name, type, country, city, domain, roles, mfn, language,
#:  [(contact name, mail local part, title)])
#: language: en / es / pt - what the requests are written in.
AGENTS = (
    # Chile
    ("Andes Cumbre", "Andes Cumbre Logistica SpA", "Agent", "Chile", "Santiago", "andescumbre.example",
     "agent", 1, "es", [("Camila Orrego", "camila.orrego", "Import Coordinator"),
                        ("Tomas Vidaurre", "tomas.vidaurre", "Commercial Lead")]),
    ("Pacifico Austral", "Pacifico Austral Cargo Ltda", "Agent", "Chile", "Valparaiso", "pacificoaustral.example",
     "agent", 1, "en", [("Josefa Larrain", "jlarrain", "Pricing Analyst"),
                        ("Benjamin Ugarte", "bugarte", "Operations")]),
    ("Maipo Freight", "Maipo Freight Chile SpA", "Agent", "Chile", "Santiago", "maipofreight.example",
     "agent", 0, "es", [("Valentina Rencoret", "vrencoret", "Sales Executive")]),
    # Peru
    ("Rimac Carga", "Rimac Carga SAC", "Agent", "Peru", "Lima", "rimaccarga.example",
     "agent", 1, "es", [("Mariela Chumpitaz", "mchumpitaz", "Customer Service"),
                        ("Jorge Taboada", "jtaboada", "Managing Partner")]),
    ("Inca Puerto", "Inca Puerto Logistics SAC", "Agent", "Peru", "Callao", "incapuerto.example",
     "agent", 0, "en", [("Diego Zevallos", "dzevallos", "Pricing")]),
    # Brazil
    ("Tupa Cargas", "Tupa Cargas Internacionais Ltda", "Agent", "Brazil", "Sao Paulo", "tupacargas.example",
     "agent", 1, "pt", [("Fernanda Albuquerque", "fernanda.albuquerque", "Pricing Coordinator"),
                        ("Rafael Tinoco", "rafael.tinoco", "Sales Director")]),
    ("Serra Azul", "Serra Azul Logistica Ltda", "Agent", "Brazil", "Santos", "serraazul-log.example",
     "agent", 1, "pt", [("Juliana Paiva", "juliana.paiva", "Import Desk")]),
    ("Paraty Log", "Paraty Trade Logistica", "Agent", "Brazil", "Itajai", "paratylog.example",
     "agent", 0, "en", [("Thiago Brandao", "thiago", "Commercial")]),
    # Mexico
    ("Altiplano Carga", "Altiplano Carga SA de CV", "Agent", "Mexico", "Mexico City", "altiplanocarga.example",
     "agent", 1, "es", [("Ximena Cordero", "xcordero", "Key Account"),
                        ("Arturo Villasenor", "avillasenor", "Ops Lead")]),
    ("Bajio Logistica", "Bajio Logistica Integral SA de CV", "Agent", "Mexico", "Guadalajara", "bajiologistica.example",
     "agent", 0, "en", [("Paola Arredondo", "parredondo", "Pricing")]),
    # Colombia
    ("Cafetal Cargo", "Cafetal Cargo SAS", "Agent", "Colombia", "Bogota", "cafetalcargo.example",
     "agent", 1, "es", [("Andres Galvis", "agalvis", "Pricing Lead"),
                        ("Natalia Restrepo", "nrestrepo", "Customer Service")]),
    ("Magdalena Freight", "Magdalena Freight SAS", "Agent", "Colombia", "Barranquilla", "magdalenafreight.example",
     "agent", 0, "en", [("Sebastian Duque", "sduque", "Sales")]),
    # Uruguay
    ("Rambla Logistics", "Rambla Logistics SRL", "Agent", "Uruguay", "Montevideo", "ramblalogistics.example",
     "agent", 1, "es", [("Florencia Aguerre", "faguerre", "Operations")]),
    # Asia
    ("Huangpu Bridge", "Huangpu Bridge Logistics Co Ltd", "Agent", "China", "Shanghai", "huangpubridge.example",
     "agent", 1, "en", [("Wendy Qian", "wendy.qian", "Overseas Sales"), ("Leo Fang", "leo.fang", "Pricing")]),
    ("Pearl Delta", "Pearl Delta Forwarding Ltd", "Agent", "China", "Shenzhen", "pearldelta-fwd.example",
     "agent", 1, "en", [("Cindy Luo", "cindy", "Overseas Desk")]),
    ("Saigon Lotus", "Saigon Lotus Logistics JSC", "Agent", "Vietnam", "Ho Chi Minh City", "saigonlotus.example",
     "agent", 0, "en", [("Tran Minh Khoa", "khoa.tran", "Sales Lead")]),
    ("Coromandel Shipping", "Coromandel Shipping Services Pvt Ltd", "Agent", "India", "Chennai",
     "coromandelship.example", "agent", 1, "en", [("Karthik Raman", "karthik.r", "Pricing")]),
    # Europe
    ("Maasvlakte Forwarding", "Maasvlakte Forwarding BV", "Agent", "Netherlands", "Rotterdam",
     "maasvlakte-fwd.example", "agent", 1, "en", [("Sanne Vermeulen", "s.vermeulen", "Ocean Export")]),
    ("Elbe Nord", "Elbe Nord Spedition GmbH", "Agent", "Germany", "Hamburg", "elbenord.example",
     "agent", 1, "en", [("Lars Hoffmeister", "l.hoffmeister", "Overseas")]),
    ("Turia Cargo", "Turia Cargo SL", "Agent", "Spain", "Valencia", "turiacargo.example",
     "agent", 0, "en", [("Irene Castello", "icastello", "Pricing")]),
    # USA and Canada
    ("Harborview Customs", "Harborview Customs Brokerage LLC", "Agent", "USA", "Chicago",
     "harborviewcustoms.example", "broker", 0, "en", [("Megan Kowalczyk", "megan", "Licensed Customs Broker")]),
    ("Prairie Crossdock", "Prairie Crossdock & Warehousing Inc", "Agent", "USA", "Bolingbrook",
     "prairiecrossdock.example", "warehouse|trucker", 0, "en", [("Dwayne Hollis", "dispatch", "Warehouse Manager")]),
    ("Lakehead Freight", "Lakehead Freight Canada Inc", "Agent", "Canada", "Toronto", "lakeheadfreight.example",
     "agent", 1, "en", [("Graham Tully", "gtully", "Transborder Sales")]),
)

DIRECT_CUSTOMERS = (
    ("Brightwater Valves", "Brightwater Valves Inc", "Direct Customer", "USA", "Milwaukee",
     "brightwatervalves.example", "direct_customer", None, "en",
     [("Heather Lindqvist", "hlindqvist", "Logistics Manager")]),
    ("Oakmont Solar", "Oakmont Solar Supply LLC", "Direct Customer", "USA", "Columbus",
     "oakmontsolar.example", "direct_customer", None, "en",
     [("Marcus Delacroix", "mdelacroix", "Supply Chain Lead")]),
)

CARRIERS = (
    ("ZIM", "ZIM Integrated Shipping Services", "Carrier", "USA", "Chicago", "zim-line.example",
     "carrier_ocean", None, "en", [("Stella Marek", "stella.marek", "Sales Executive, Midwest")]),
    ("Korean Air", "Korean Air Cargo", "Carrier", "South Korea", "Seoul", "koreanair-cargo.example",
     "carrier_air", None, "en", [("Baek Jin-ho", "jinho.baek", "Cargo Sales ORD")]),
    ("Blue Mesa Transport", "Blue Mesa Transport LLC", "Carrier", "USA", "Dallas", "bluemesatransport.example",
     "trucker", None, "en", [("Travis Oakes", "loads", "Dispatcher")]),
)

#: The trucker that was booked first on the truckload and replaced after a
#: missed pickup. Not a party: it only lives in the shipment's history.
REPLACED_TRUCKER = "Cedar Ridge Trucking"

#: Our offices: (code, city, country, [(contact, local part, title)]).
OFFICES = (
    ("JJC", "Chicago", "USA", [("Iris Chen", "iris.chen", "Inside Sales"),
                               ("Victor Lang", "victor.lang", "Branch Manager"),
                               ("Ava Tran", "ava.tran", "Operations"),
                               ("Colin Hart", "colin.hart", "Sales")]),
    ("JQM", "Miami", "USA", [("Lucia Reyna", "lucia.reyna", "LATAM Desk")]),
    ("JJH", "Hong Kong", "Hong Kong", [("Daniel Yip", "daniel.yip", "Overseas Sales")]),
    ("JJS", "Shanghai", "China", [("Oscar Peng", "oscar.peng", "Overseas Sales")]),
    ("JJB", "Bangalore", "India", [("Rohan Iyer", "rohan.iyer", "Air Export")]),
)

#: Who signs our mail: office code -> name as the signature and the
#: display name spell it.
STAFF_TITLES = {"Iris Chen": "Inside Sales", "Victor Lang": "Branch Manager",
                "Ava Tran": "Operations", "Colin Hart": "Sales"}

#: Phone numbers are 555-01xx: fictional by convention.
OFFICE_PHONES = {"JJC": "+1 312-555-0142", "JQM": "+1 305-555-0117", "JJH": "+852 555-0163",
                 "JJS": "+86 21 555-0129", "JJB": "+91 80 555-0188"}
COUNTRY_CALLING = {"Chile": "+56 2", "Peru": "+51 1", "Brazil": "+55 11", "Mexico": "+52 55",
                   "Colombia": "+57 1", "Uruguay": "+598 2", "China": "+86 21", "Vietnam": "+84 28",
                   "India": "+91 44", "Netherlands": "+31 10", "Germany": "+49 40", "Spain": "+34 96",
                   "USA": "+1 312", "Canada": "+1 416", "South Korea": "+82 2"}

# ----------------------------------------------------------------- the lanes
#: Where each kind of requester ships from and to.
US_OCEAN = ("CHICAGO, IL", "DETROIT, MI", "INDIANAPOLIS, IN", "COLUMBUS, OH", "MILWAUKEE, WI",
            "HOUSTON, TX", "SAVANNAH, GA", "MIAMI, FL", "NEWARK, NJ", "CHARLESTON, SC", "MEMPHIS, TN",
            "BOLINGBROOK, IL", "LOS ANGELES, CA")
US_AIR = ("ORD", "ORD", "ORD", "MIA", "JFK", "LAX", "ATL", "IAH", "DFW")
US_DOMESTIC = ("CHICAGO, IL", "BOLINGBROOK, IL", "DALLAS, TX", "ATLANTA, GA", "MEMPHIS, TN",
               "COLUMBUS, OH", "LAREDO, TX", "HOUSTON, TX", "DETROIT, MI", "NEWARK, NJ")
LATAM_OCEAN = {
    "Chile": ("SAN ANTONIO", "SAN ANTONIO", "VALPARAISO", "IQUIQUE", "ANTOFAGASTA", "SANTIAGO"),
    "Peru": ("CALLAO", "CALLAO", "PAITA", "LIMA"),
    "Brazil": ("SANTOS", "SANTOS", "ITAJAI", "PARANAGUA", "RIO DE JANEIRO", "SAO PAULO", "MANAUS"),
    "Mexico": ("MANZANILLO", "VERACRUZ", "LAZARO CARDENAS", "MONTERREY", "GUADALAJARA", "MEXICO CITY"),
    "Colombia": ("CARTAGENA", "BUENAVENTURA", "BARRANQUILLA", "BOGOTA", "MEDELLIN"),
    "Uruguay": ("MONTEVIDEO",),
}
LATAM_AIR = {"Chile": ("SCL",), "Peru": ("LIM",), "Brazil": ("GRU", "VCP"), "Mexico": ("MEX",),
             "Colombia": ("BOG",), "Uruguay": ("MVD",)}
ASIA_OCEAN = {"China": ("SHANGHAI", "NINGBO", "YANTIAN", "QINGDAO", "XIAMEN"),
              "Vietnam": ("HO CHI MINH CITY",), "India": ("CHENNAI", "NHAVA SHEVA"),
              "Hong Kong": ("HONG KONG",)}
ASIA_AIR = {"China": ("PVG",), "Hong Kong": ("HKG",), "India": ("BLR",), "Vietnam": ("SGN",)}
EUROPE_OCEAN = {"Netherlands": ("ROTTERDAM", "ANTWERP"), "Germany": ("HAMBURG",), "Spain": ("VALENCIA",)}
EUROPE_AIR = {"Netherlands": ("FRA",), "Germany": ("FRA",), "Spain": ("MADRID",)}

COMMODITIES = (
    "auto parts", "industrial pumps", "solar panels", "machinery spare parts", "copper fittings",
    "medical devices", "plastic resin", "paper rolls", "HVAC units", "tires", "furniture",
    "electronic components", "agricultural sprayers", "valves and flanges", "packaging film",
    "lab equipment", "mining drill bits", "frozen berries", "wine", "coffee", "textile yarn",
    "cosmetics", "steel wire", "power tools", "LED lighting", "water filters", "printing ink",
)
REEFER_COMMODITIES = ("frozen berries", "wine")
END_CUSTOMERS = (
    "Cordillera Norte Mining", "Vitalis Clinics", "Granja Solaz", "Norvale Agro", "Castell Industrial",
    "Helios Farms", "Minera Punta Seca", "Arcadia Home", "Quanta Labs", "Terranova Ceramics",
    "Rio Claro Foods", "Albor Energia", "Delmar Plastics", "Solis Motors", "Vireo Packaging",
)

#: Invented vessel names and flight numbers for the shipments.
VESSELS = ("ZIM CORAL BAY / 118S", "ZIM SAPPHIRE KEY / 042S")
