# -*- coding: utf-8 -*-

import os
import tempfile
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError, ValidationError


@tagged('post_install', '-at_install', 'studio_to_module')
class TestStudioToModule(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        
        # Create a test Studio view
        cls.test_view = cls.env['ir.ui.view'].create({
            'name': 'Test Studio View',
            'model': 'res.partner',
            'arch': '''
                <form>
                    <field name="name"/>
                    <field name="email"/>
                </form>
            ''',
            'studio': True,
        })
        
        # Get or create a test module
        cls.test_module = cls.env['ir.module.module'].search([
            ('name', '=', 'base'),
            ('state', '=', 'installed')
        ], limit=1)

    def test_01_studio_view_detection(self):
        """Test that Studio views are properly detected"""
        self.assertTrue(self.test_view.is_studio_view)
        self.assertFalse(self.test_view.converted_to_module)
        self.assertFalse(self.test_view.pending_cleanup)

    def test_02_mark_for_conversion(self):
        """Test marking a view for conversion"""
        self.test_view.mark_for_conversion(self.test_module)
        
        self.assertTrue(self.test_view.converted_to_module)
        self.assertEqual(self.test_view.target_module_id, self.test_module)
        self.assertTrue(self.test_view.pending_cleanup)

    def test_03_sanitize_xml_id(self):
        """Test XML ID sanitization"""
        wizard = self.env['studio.view.converter'].create({
            'target_module_id': self.test_module.id,
        })
        
        # Test various name formats
        test_cases = [
            ('Test View', 'test_view'),
            ('Test  Multiple   Spaces', 'test_multiple_spaces'),
            ('Test-Dash-View', 'test_dash_view'),
            ('Test.Dot.View', 'test_dot_view'),
            ('Test (Parentheses)', 'test_parentheses'),
            ('123 Number Start', '123_number_start'),
        ]
        
        for name, expected in test_cases:
            result = wizard._sanitize_xml_id(name)
            self.assertEqual(result, expected, f"Failed for '{name}': got '{result}', expected '{expected}'")

    def test_04_generate_view_xml(self):
        """Test XML generation from view"""
        wizard = self.env['studio.view.converter'].create({
            'target_module_id': self.test_module.id,
        })
        
        xml_content = wizard._generate_view_xml(self.test_view)
        
        # Check that XML contains essential elements
        self.assertIn('<record id=', xml_content)
        self.assertIn('model="ir.ui.view"', xml_content)
        self.assertIn(self.test_view.name, xml_content)
        self.assertIn(self.test_view.model, xml_content)
        self.assertIn('<field name="arch"', xml_content)

    def test_05_wizard_validation(self):
        """Test wizard validation"""
        wizard = self.env['studio.view.converter'].create({})
        
        # Should fail without views
        with self.assertRaises(ValidationError):
            wizard.action_convert_views()
        
        # Should fail without target module
        wizard.studio_view_ids = [(6, 0, [self.test_view.id])]
        with self.assertRaises(ValidationError):
            wizard.action_convert_views()

    def test_06_cleanup_converted_views(self):
        """Test cleanup of converted views"""
        # Create a test view and mark it for conversion
        test_view = self.env['ir.ui.view'].create({
            'name': 'Test Cleanup View',
            'model': 'res.partner',
            'arch': '<form><field name="name"/></form>',
            'studio': True,
        })
        
        test_view.mark_for_conversion(self.test_module)
        view_id = test_view.id
        
        # Run cleanup
        self.env['ir.ui.view'].cleanup_converted_views()
        
        # View should be deleted
        self.assertFalse(self.env['ir.ui.view'].browse(view_id).exists())

    def test_07_wizard_default_get(self):
        """Test wizard default values from context"""
        # Create wizard with view in context
        wizard = self.env['studio.view.converter'].with_context(
            active_model='ir.ui.view',
            active_ids=[self.test_view.id]
        ).create({})
        
        # Should pre-select the view
        self.assertIn(self.test_view, wizard.studio_view_ids)

    def test_08_inherit_view_xml_generation(self):
        """Test XML generation for inherited views"""
        # Create a parent view
        parent_view = self.env['ir.ui.view'].create({
            'name': 'Parent View',
            'model': 'res.partner',
            'arch': '<form><field name="name"/></form>',
        })
        
        # Create an inherited Studio view
        inherited_view = self.env['ir.ui.view'].create({
            'name': 'Inherited Studio View',
            'model': 'res.partner',
            'inherit_id': parent_view.id,
            'arch': '<field name="name" position="after"><field name="email"/></field>',
            'studio': True,
        })
        
        wizard = self.env['studio.view.converter'].create({
            'target_module_id': self.test_module.id,
        })
        
        xml_content = wizard._generate_view_xml(inherited_view)
        
        # Check that inherit_id is included
        self.assertIn('inherit_id', xml_content)
        self.assertIn('ref=', xml_content)

    def test_09_multiple_views_same_model(self):
        """Test handling multiple views for the same model"""
        # Create multiple Studio views for the same model
        view1 = self.env['ir.ui.view'].create({
            'name': 'Partner View 1',
            'model': 'res.partner',
            'arch': '<form><field name="name"/></form>',
            'studio': True,
        })
        
        view2 = self.env['ir.ui.view'].create({
            'name': 'Partner View 2',
            'model': 'res.partner',
            'arch': '<tree><field name="name"/></tree>',
            'studio': True,
        })
        
        wizard = self.env['studio.view.converter'].create({
            'studio_view_ids': [(6, 0, [view1.id, view2.id])],
            'target_module_id': self.test_module.id,
        })
        
        # Should group by model
        views_by_model = {}
        for view in wizard.studio_view_ids:
            if view.model not in views_by_model:
                views_by_model[view.model] = []
            views_by_model[view.model].append(view)
        
        self.assertEqual(len(views_by_model['res.partner']), 2)
