DROP TABLE IF EXISTS products;
CREATE TABLE products (
  id INT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(255),
  description VARCHAR(1000),
  vec MYVECTOR(type=HNSW,dim=5,size=1000,M=16,ef=50,ef_search=32,dist=L2)
);

INSERT INTO products(name,description,vec) VALUES
('RainShell Pro','Waterproof breathable hiking jacket with taped seams, hood and packable design.',myvector_construct('[1.0,1.0,0.2,0.1,0.3]')),
('TrailWarm Fleece','Warm lightweight fleece midlayer for cool mountain hikes and outdoor travel.',myvector_construct('[0.1,0.9,0.8,0.2,0.5]')),
('StormPack 28','Weather-resistant daypack with laptop sleeve and rain cover for hiking and commuting.',myvector_construct('[0.8,0.2,0.3,0.7,0.4]')),
('Camp Brew Kit','Compact coffee brewer, insulated mug and hand grinder for coffee outdoors.',myvector_construct('[0.1,0.1,0.9,0.8,0.2]')),
('TravelCharge 65','Small 65W USB-C charger with two ports for phones, tablets and laptops while travelling.',myvector_construct('[0.1,0.1,0.2,0.9,1.0]')),
('Nomad Power Bank','Slim high-capacity USB-C battery pack for long flights, camping and remote work.',myvector_construct('[0.1,0.1,0.2,0.8,0.9]')),
('Alpine Gloves','Insulated touchscreen gloves designed for cold-weather hiking and cycling.',myvector_construct('[0.2,1.0,0.7,0.1,0.4]')),
('City Raincoat','Lightweight hooded raincoat for daily commuting and wet-weather city walks.',myvector_construct('[0.9,0.4,0.1,0.4,0.2]'));

CALL mysql.myvector_index_build('vectordb.products.vec','id');
