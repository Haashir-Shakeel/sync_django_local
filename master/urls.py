from django.urls import path
from . import views
from .sync_views import SyncPushView, SyncPullView

urlpatterns = [
    path('sync/push/', SyncPushView.as_view(), name='sync_push'),
    path('sync/pull/', SyncPullView.as_view(), name='sync_pull'),
]
