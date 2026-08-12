# dados/

As pastas estão versionadas, os arquivos não. Quem clona recebe a estrutura
vazia e precisa colocar as exportações do SAP nos lugares certos.

## O que vai em cada pasta

| Pasta | Arquivo esperado | Usado por |
|---|---|---|
| `clientes/` | `clientes_origem.xlsx` | preparar clientes |
| `ilhas_vendedores/` | `vendedores.xlsx` | preparar clientes (join) |
| `faturamento/` | `faturamento_origem.csv` | preparar faturamento |
| `itens/` | `itens_origem.csv` | preparar itens |
| `preco_revenda/` | `precos.csv` | tabela de referência |
| `imagem_url/` | qualquer `.csv` | tabela de referência |
| `referencia/` | `clientes_origem.xlsx` (layout técnico do SAP) | só quando a origem vem com cabeçalho em português |
| `para_vps/` | gerado pelo `preparar` | o `upload` lê daqui |
| `_backup/` | gerado pelo `upload` | arquivo já carregado vai pra cá |

Os nomes de arquivo estão em `config/settings.py` (`ORIGENS`), e dá pra apontar
pra outro lugar com `ETL_PASTA_DADOS` no `.env`.

## para_vps/

É saída, não entrada — o `preparar` cria as subpastas sozinho. **O nome da
subpasta vira o nome da tabela no MySQL** (`Faturamento` → tabela
`Faturamento`), então renomear pasta aqui renomeia tabela lá.

A quantidade de `Itens Extra N` varia conforme o número de colunas que a
origem manda, por isso essas não estão versionadas.

## Por que nada de dado entra no git

Os arquivos passam de 130 MB e têm dado pessoal de cliente — CNPJ/CPF, e-mail,
telefone, endereço. Repositório não é lugar pra isso.
