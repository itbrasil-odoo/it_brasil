import logging
from odoo import models, _
from lxml import objectify
from odoo.exceptions import UserError,ValidationError


_logger = logging.getLogger(__name__)


def convert(obj, conversion=None):
    if conversion:
        return conversion(obj.text)
    if isinstance(obj, objectify.StringElement):
        return str(obj)
    if isinstance(obj, objectify.IntElement):
        return int(obj)
    if isinstance(obj, objectify.FloatElement):
        return float(obj)
    raise u"Tipo não implementado %s" % str(type(obj))


def get(obj, path, conversion=None):
    paths = path.split(".")
    index = 0
    for item in paths:
        if not item:
            continue
        if hasattr(obj, item):
            obj = obj[item]
            index += 1
        else:
            return None
    if len(paths) == index:
        return convert(obj, conversion=conversion)
    return None


def remove_none_values(dict):
    res = {}
    res.update({k: v for k, v in dict.items() if v})
    return res


def cnpj_cpf_format(cnpj_cpf):
    if len(cnpj_cpf) == 14:
        cnpj_cpf = (cnpj_cpf[0:2] + '.' + cnpj_cpf[2:5] +
                    '.' + cnpj_cpf[5:8] +
                    '/' + cnpj_cpf[8:12] +
                    '-' + cnpj_cpf[12:14])
    else:
        cnpj_cpf = (cnpj_cpf[0:3] + '.' + cnpj_cpf[3:6] +
                    '.' + cnpj_cpf[6:9] + '-' + cnpj_cpf[9:11])
    return cnpj_cpf
 


class AccountMove(models.Model):
    _inherit = 'account.move'

    """ ================================================
                        Validado    
    ================================================="""

    def import_nfe(self, company_id, nfe, xml):
        _logger.info(["import_nfe"])
        
        if self.search([('document_key', '=', nfe.protNFe.infProt.chNFe.text)]):
            raise UserError('Documento Eletrônico já importado!')

        invoice = {
            # Campos obrigatórios
            "invoice_date": nfe.NFe.infNFe.ide.dhEmi.text,
            "date": nfe.NFe.infNFe.ide.dhEmi.text,
            "company_id": company_id.id,
            "currency_id": company_id.currency_id.id,
            "journal_id": self._search_default_journal(["purchase"]).id,
            "move_type": "in_invoice",
            "state": "draft",
            "state_edoc": "em_digitacao",
            "fiscal_operation_id": self.env["l10n_br_fiscal.operation"].search([("fiscal_type","=","purchase")], limit=1).id,

            # Não obrigatórios
            "document_type_id": company_id.document_type_id.id,
            "document_number": nfe.NFe.infNFe.ide.nNF.text,
            "document_key": nfe.protNFe.infProt.chNFe.text,
            "document_serie": nfe.NFe.infNFe.ide.serie.text,
        }

        invoice.update(self._get_company_invoice(nfe))
        invoice.update(self.get_partner_nfe(nfe))

        _logger.info(["Criando Fatura"])
        invoice = self.create(invoice)

        _logger.info(["Criando Att Xml"])
        xml_file_vals = {"name": f"NFe-{nfe.protNFe.infProt.chNFe.text}.xml", "datas": xml}
        xml_file = self.env["ir.attachment"].create(xml_file_vals)

        _logger.info(["Criando Evento de Autorização"])
        vals_event = {
            "company_id": company_id.id,
            "document_id": invoice.fiscal_document_id.id,
            "document_type_id": company_id.document_type_id.id,
            "document_number": nfe.NFe.infNFe.ide.nNF.text,
            "document_serie_id": self.env["l10n_br_fiscal.document.serie"].search([('code','=','1')], limit=1).id,
            "partner_id": invoice.partner_id.id or False,
            "protocol_number": nfe.protNFe.infProt.nProt.text,
            "file_response_id": xml_file.id,
            "file_request_id": xml_file.id
        }
        authorization_event = self.env["l10n_br_fiscal.event"].create(vals_event)

        _logger.info(["Atualizando Fatura"])
        invoice.update({"authorization_event_id": authorization_event.id})

        _logger.info(["Criando Linha Da Fatura"])
        debit_itens = []
        credit_total = 0
        for line in nfe.NFe.infNFe.det: 
            credit_total += round(line.prod.qCom * line.prod.vUnCom + (line.imposto.IPI.IPITrib.vIPI if hasattr(line.imposto, "IPI") else 0), 3)
            product_debit = self.create_invoice_item(line, invoice)
            debit_itens.append([0,0, product_debit])

        # TODO Diferença centavos
        """ if nfe.NFe.infNFe.total.ICMSTot.vNF != credit_total:
            return """

        if hasattr(nfe.NFe.infNFe, "cobr"):
            for line in nfe.NFe.infNFe.cobr.dup:
                product_credit = {
                    "name": False,
                    'move_id': invoice.id, 
                    'quantity': 1,
                    "fiscal_quantity": 1,
                    'currency_id': invoice.company_id.currency_id.id,
                    "debit": 0,
                    'credit': line.vDup , 
                    'date_maturity': str(line.dVenc),
                    "fiscal_price": - line.vDup,
                    "exclude_from_invoice_tab": True,
                    'account_id': invoice.partner_id.property_account_payable_id.id,
                }
                debit_itens.append([0,0, product_credit])
        else:
            product_credit = {
                    "name": False,
                    'move_id': invoice.id, 
                    'quantity': 1,
                    "fiscal_quantity": 1,
                    'currency_id': invoice.company_id.currency_id.id,
                    "debit": 0,
                    'credit': round(credit_total, 2),
                    'date_maturity': str(nfe.NFe.infNFe.ide.dhEmi.text),
                    "fiscal_price": - round(credit_total, 2),
                    "exclude_from_invoice_tab": True,
                    'account_id': invoice.partner_id.property_account_payable_id.id,
                }
            debit_itens.append([0,0, product_credit])
            invoice.update({"move_type": "in_refund"})    
        invoice.line_ids = debit_itens
        for line in invoice.invoice_line_ids: 
            taxes = line._get_computed_taxes()
            if taxes and line.move_id.fiscal_position_id:
                taxes = line.move_id.fiscal_position_id.map_tax(taxes, partner=line.partner_id)
            line.tax_ids = taxes
        return invoice

    def create_invoice_item(self, item, invoice):
        codigo = get(item.prod, 'cProd', str)
        seller_id = self.env['product.supplierinfo'].search([('name', '=', invoice.partner_id.id),('product_code', '=', codigo)])

        product_id = None
        if seller_id:
            product_id = seller_id.product_tmpl_id
            if len(product_id) > 1:
                message = '\n'.join(["Produto: %s - %s" % (x.default_code or '', x.name) for x in product_id])
                raise UserError("Existem produtos duplicados com mesma codificação, corrija-os antes de prosseguir:\n%s" % message)

        if not product_id and item.prod.cEAN and str(item.prod.cEAN) != 'SEM GTIN':
            product_id = self.env['product.product'].search(
                [('barcode', '=', item.prod.cEAN)], limit=1)
        
        if not product_id:
            raise UserError("Não existe nenhum produto cadatrado com o código: %s" % codigo)
 
        product_debit = {
            'name': product_id.name, 
            'move_id': invoice.id, 
            'product_id': product_id.id,
            'quantity': item.prod.qCom,
            'fiscal_quantity': item.prod.qCom,
            'currency_id': invoice.company_id.currency_id.id,
            'credit': 0,
            'debit': round(item.prod.qCom * item.prod.vUnCom + (item.imposto.IPI.IPITrib.vIPI if hasattr(item.imposto, "IPI") else 0), 2),
            'exclude_from_invoice_tab': False,
            'price_unit': item.prod.vUnCom,
            'fiscal_price': item.prod.vUnCom,
            'cfop_id':  self.env["l10n_br_fiscal.cfop"].search([('code','=', item.prod.CFOP)], limit=1).id,
            'account_id': product_id.categ_id.property_account_expense_categ_id.id,
            'ncm_id': product_id.ncm_id.id,
            'fiscal_operation_id': self.env["l10n_br_fiscal.operation"].search([("fiscal_type","=","purchase")], limit=1).id,
        }   

        if hasattr(item.imposto, 'ICMS'):
            product_debit.update(self._get_icms(item.imposto)) 

        if hasattr(item.imposto, 'IPI'):
            product_debit.update(self._get_ipi(item.imposto.IPI))

        product_debit.update(self._get_pis(item.imposto.PIS))
        product_debit.update(self._get_cofins(item.imposto.COFINS))

        return product_debit

    def _get_company_invoice(self, nfe):
        dest_cnpj_cpf = cnpj_cpf_format(str(nfe.NFe.infNFe.dest.CNPJ.text).zfill(14))
        company = self.env['res.company'].sudo().search([('partner_id.cnpj_cpf', '=', dest_cnpj_cpf)])

        if not company: 
            raise UserError("XML não destinado nem emitido por esta empresa.")
        return dict(company_id=company.id,)

    def get_partner_nfe(self, nfe):
        cnpj_cpf = cnpj_cpf_format(str(nfe.NFe.infNFe.emit.CNPJ.text).zfill(14))
        partner_id = self.env['res.partner'].search([('cnpj_cpf', '=', cnpj_cpf)], limit=1)        
        if not partner_id:
            raise ValidationError(_("Parceiro não cadastrado"))
        
        return dict(partner_id=partner_id.id)


    def _get_icms(self, imposto):
        csts = ['00', '10', '20', '30', '40', '41', '50',
                '51', '60', '70', '90']
        csts += ['101', '102', '103', '201', '202', '203',
                 '300', '400', '500', '900']

        cst_item = None
        vals = {}

        for cst in csts:
            tag_icms = None
            if hasattr(imposto.ICMS, 'ICMSSN%s' % cst):
                tag_icms = 'ICMSSN'
                cst_item = get(imposto, 'ICMS.ICMSSN%s.CSOSN' % cst, str)
            elif hasattr(imposto.ICMS, 'ICMS%s' % cst):
                tag_icms = 'ICMS'
                cst_item = get(imposto, 'ICMS.ICMS%s.CST' % cst, str)
                cst_item = str(cst_item).zfill(2)
            if tag_icms:
                icms = imposto.ICMS
                vals = { 
                    'icms_origin': get(
                        icms, '%s%s.orig' % (tag_icms, cst), str),
                    'icms_base_type': get(
                        icms, '%s%s.modBC' % (tag_icms, cst), str), 
                    'icms_base': get(
                        icms, '%s%s.vBC' % (tag_icms, cst)),
                    'icms_reduction': get(
                        icms, '%s%s.pRedBC' % (tag_icms, cst)),
                    'icms_percent': get(
                        icms, '%s%s.pICMS' % (tag_icms, cst)),
                    'icms_value': get(
                        icms, '%s%s.vICMS' % (tag_icms, cst)),
                }

        return remove_none_values(vals)

    
    def _get_ipi(self, ipi):
        vals = {}
        for item in ipi.getchildren():
            vals = { 
                'ipi_base': get(ipi, '%s.vBC' % item.tag[36:]),
                'ipi_percent': get(ipi, '%s.pIPI' % item.tag[36:]),
                'ipi_value': get(ipi, '%s.vIPI' % item.tag[36:]), 
            }

        return remove_none_values(vals)


    def _get_pis(self, pis):
        vals = {}
        for item in pis.getchildren():
            vals = { 
                'pis_base': get(pis, '%s.vBC' % item.tag[36:]),
                'pis_percent': get(pis, '%s.pPIS' % item.tag[36:]),
                'pis_value': get(pis, '%s.vPIS' % item.tag[36:]),
            }

        return remove_none_values(vals)

    def _get_cofins(self, cofins):
        vals = {}
        for item in cofins.getchildren():
            vals = { 
                'cofins_base': get(cofins, '%s.vBC' % item.tag[36:]),
                'cofins_percent': get(cofins, '%s.pCOFINS' % item.tag[36:]),
                'cofins_value': get(cofins, '%s.vCOFINS' % item.tag[36:]),
            }

        return remove_none_values(vals)