# Inventário agroflorestal por drone

Conta e localiza as plantas de um sistema agroflorestal a partir de uma ortofoto de drone.
Entra a imagem, sai um mapa com cada planta marcada e um arquivo geográfico para usar no seu SIG.

![Mapa com a ortofoto e as detecções](docs/mapa.png)

Roda **na sua máquina**, sem GPU, sem conta em serviço nenhum e sem enviar a sua imagem para
lugar algum. Depois de baixar os modelos, funciona sem internet.

---

## Como usar

```bash
git clone https://github.com/COURAGEOUS-LAND/agroforestry-inventory
cd agroforestry-inventory

pip install -r requirements.txt
python baixar_modelos.py

python inventario.py exemplo/cafe_raizes.tif --especie cafe
```

O navegador abre sozinho com o mapa. A ortofoto de exemplo está no repositório, então dá para
ver funcionando antes de usar a sua própria imagem.

Com a sua ortofoto, é o mesmo comando:

```bash
python inventario.py /caminho/da/minha_ortofoto.tif --especie banana
```

### Enquanto o repositório for privado

O `baixar_modelos.py` busca os pesos por URL direta, e asset de release privada não responde a
URL direta — nem com token, só com sessão de navegador. Então, para quem é da Courageous Land,
a linha do download muda:

```bash
gh auth login                                       # uma vez, se ainda não fez
gh release download modelos-v1.0.0 --dir modelos    # no lugar de baixar_modelos.py
python baixar_modelos.py                            # não baixa nada: só confere os sha256
```

A segunda linha traz os quatro `.pth`; a terceira reconhece que já estão no lugar e verifica a
integridade de cada um, que é o que interessa — download truncado carrega e infere errado sem
reclamar.

Sem o `gh`, dá para baixar os quatro pesos pela
[página da release](https://github.com/courageous-land/agroforestry-inventory/releases/tag/modelos-v1.0.0)
no navegador e apontar a pasta:

```bash
python baixar_modelos.py --de /caminho/da/pasta/com/os/pesos
```

Quando o repositório abrir, esta seção sai e o `python baixar_modelos.py` do bloco lá de cima
passa a funcionar sozinho, sem nenhuma outra mudança.

### O que sai

| arquivo | o quê |
|---|---|
| `saidas/<nome>_<especie>_caixas.geojson` | um polígono por planta, com a confiança |
| `saidas/<nome>_<especie>_centroides.geojson` | o ponto central de cada planta |
| `saidas/<nome>_<especie>.csv` | a mesma coisa em tabela, com latitude e longitude |

Tudo em EPSG:4326, pronto para abrir no QGIS.

---

## Modelos disponíveis

Cada modelo detecta **uma** espécie. Os números abaixo foram medidos comparando as detecções com
plantas marcadas à mão, em áreas que o modelo nunca viu durante o treino.

| espécie | `--especie` | precisão | recall | erro de posição | copa mediana |
|---|---|---|---|---|---|
| Pitaia | `pitaia` | 0,89 | 0,98 | 7 cm | 0,97 m |
| Café arábica | `cafe` | 0,72 | 0,91 | 3 cm | 0,36 m |
| Abacate | `abacate` | 0,69 | 0,94 | 12 cm | 1,52 m |
| Banana | `banana` | 0,59 | 0,93 | 18 cm | 3,11 m |

**Precisão** é quantas das plantas apontadas são reais. **Recall** é quantas das plantas reais
foram encontradas. Os dois importam, e por motivos diferentes: precisão baixa infla a contagem,
recall baixo esconde plantas.

Cada modelo traz também um campo `onde_falha` em [`modelos.json`](modelos.json), dizendo em que
situação ele erra. Vale ler antes de usar.

---

## Leia isto antes de confiar num número

**Estes modelos não transferem de um lugar para outro sem perda.** Não é uma ressalva de praxe —
está medido, e a falha é silenciosa.

Um dos nossos modelos de café obteve pontuação alta na própria validação. Aplicado a áreas
retidas da **mesma ortofoto**, a algumas centenas de metros de onde foi treinado, encontrou
**2 de 198** plantas. E a precisão dele continuou em 1,00: as duas que achou estavam certas.
Ou seja, ele não inventou nada — ficou cego, e **nada na saída avisava**.

O motivo é que uma agrofloresta muda muito: altura de voo, hora do dia, estação, idade da planta,
solo, sombreamento e espaçamento mudam a aparência da copa mais do que se imagina.

Então, ao rodar sobre a sua imagem:

1. Abra o mapa e **olhe**. Se as caixas não caem sobre as plantas, o modelo não serve ali.
2. Confira uma amostra à mão. Escolha uma área pequena, conte as plantas, compare com o número.
3. Ajuste a confiança mínima no painel e veja como a contagem se move. Se ela desaba com um
   pequeno aumento, as detecções estão frágeis.

Preferimos dizer isso na primeira página a deixar você descobrir depois.

---

## Como funciona

Três etapas, que o `inventario.py` encadeia e que também rodam sozinhas.

```
ortofoto.tif ──▶ ingerir.py ──▶ inferir.py ──▶ servidor.py ──▶ mapa no navegador
                    COG          GeoJSON         tiles + página
```

**1. `ingerir.py` — preparar a imagem.** Converte a ortofoto para *Cloud Optimized GeoTIFF*:
organizada em blocos e com uma pirâmide interna de resoluções. Sem isso, ler um pedaço da imagem
obriga o programa a percorrer o arquivo inteiro. A profundidade da pirâmide é calculada pelo
tamanho da imagem, e não fixada, para continuar funcionando quando a ortofoto for de 4 GB.

**2. `inferir.py` — encontrar as plantas.** A ortofoto é grande demais para caber num modelo de
visão, então é percorrida em recortes com sobreposição. Cada caixa devolvida em pixel vira
coordenada geográfica pela transformação afim do raster. Como os recortes se sobrepõem, a mesma
planta é detectada mais de uma vez perto das bordas; a deduplicação junta essas repetições
mantendo a de maior confiança.

**3. `servidor.py` — mostrar.** Serve a página e recorta cada tile do mapa direto do COG, na hora.
Isso dispensa gerar uma pirâmide de imagens em disco, que numa ortofoto de 1,9 GB custaria mais de
600 MB e vários minutos. Medido: 23 a 67 ms por tile novo, 2 ms quando já está em cache.

### Desempenho

Medido numa ortofoto de 1,9 GB (27.426 × 21.800 px):

| | com GPU (RTX 4070) | só CPU |
|---|---|---|
| por recorte | 17 ms | 100 ms |
| ortofoto inteira | ~1 min | **~4 min** |
| ortofoto de 4 GB | ~2 min | ~9 min |

A GPU ajuda, mas não é requisito. O projeto foi desenhado para rodar em máquina comum.

---

## Ajustes

```bash
python inventario.py imagem.tif --especie cafe \
    --confianca 0.35 \      # detecção mínima aceita (padrão 0,25)
    --dedup-m 0.4 \         # distância abaixo da qual duas detecções são a mesma planta
    --refazer               # ignora resultados anteriores
```

Sobre o `--dedup-m`: o padrão vem do manifesto, por espécie. Se for mexer, a regra prática é
**metade da menor distância real entre duas plantas** no seu plantio. Valor alto demais funde
plantas vizinhas numa só e subestima a contagem.

---

## Requisitos

Python 3.11 ou superior. Tudo instala por `pip`, sem precisar compilar GDAL nem instalar QGIS.

A ortofoto precisa ter pelo menos três bandas (RGB) e estar georreferenciada. Os modelos foram
treinados com imagens de cerca de 1,7 cm por pixel; resoluções muito diferentes degradam o
resultado.

---

## Documentação

| | |
|---|---|
| [`docs/diagrama_stack.png`](docs/diagrama_stack.png) | o stack completo: o que roda na máquina do usuário, o que fica do nosso lado e o que ainda será construído |
| [`docs/diagrama_fluxo_dados.png`](docs/diagrama_fluxo_dados.png) | o caminho do dado, e a fronteira entre o que nunca sai da máquina e o que é publicado |
| [`docs/Template2_Product_Requirements_EN.docx`](docs/Template2_Product_Requirements_EN.docx) | descrição técnica completa da solução, submetida ao UNICEF Innovation Fund |
| [`docs/Template2_Requisitos_do_Produto_PT.docx`](docs/Template2_Requisitos_do_Produto_PT.docx) | o mesmo documento em português |

---

## Licença

Código sob [GPL-3.0](LICENSE). Modelos sob CC-BY-4.0.
