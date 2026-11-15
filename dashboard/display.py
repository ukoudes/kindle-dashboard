"""Paperwhite 10 display defaults and shared image/hit-area geometry."""
from dataclasses import dataclass

DEFAULT_WIDTH = 1072
DEFAULT_HEIGHT = 1448
TABS = (("Coding Plan", "usage"), ("API", "api"))


@dataclass(frozen=True)
class Layout:
    width: int
    height: int

    @property
    def landscape(self):
        return self.width > self.height

    @property
    def logical_width(self):
        return 800 if self.landscape else 600

    @property
    def scale(self):
        return self.width / self.logical_width

    @property
    def logical_height(self):
        return self.height / self.scale

    @property
    def margin(self):
        return 36

    def tab_rect(self, index):
        cell = (self.logical_width - 2*self.margin) / len(TABS)
        return self.margin + index*cell, 102, cell, 46

    def device_button(self):
        return self.logical_width-self.margin-76,24,76,38

    def device_row(self,index):
        return self.margin,188+index*152,self.logical_width-2*self.margin,132

    def percent(self,rect):
        x,y,w,h=rect
        return x/self.logical_width*100,y/self.logical_height*100,w/self.logical_width*100,h/self.logical_height*100

    def tab_percent(self, index):
        x, y, w, h = self.tab_rect(index)
        return x/self.logical_width*100, y/self.logical_height*100, w/self.logical_width*100, h/self.logical_height*100
