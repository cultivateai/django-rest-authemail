import urllib
import json

from datetime import date

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from ipware import get_client_ip
from ratelimit.decorators import ratelimit
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from authemail.models import (AuthAuditEventType, AuthAuditLog,
                              EmailChangeCode, PasswordResetCode, SignupCode,
                              send_multi_format_email)
from authemail.serializers import (EmailChangeSerializer, LoginSerializer,
                                   PasswordChangeSerializer,
                                   PasswordResetSerializer,
                                   PasswordResetVerifiedSerializer,
                                   SignupSerializer,
                                   SignupVerificationSerializer,
                                   UserSerializer)

from django.utils.module_loading import import_string
AUTH_VERIFY_CALLBACK = getattr(settings, "AUTH_VERIFY_CALLBACK", None)
auth_verify_callback_func = None
if AUTH_VERIFY_CALLBACK is not None:
    try:
        auth_verify_callback_func = import_string(AUTH_VERIFY_CALLBACK)
    except:
        auth_verify_callback_func = None


class Signup(APIView):
    permission_classes = (AllowAny,)
    serializer_class = SignupSerializer

    def post(self, request, format=None):
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            email = serializer.data["email"]
            password = serializer.data["password"]
            first_name = serializer.data["first_name"]
            last_name = serializer.data["last_name"]

            must_validate_email = getattr(
                settings, "AUTH_EMAIL_VERIFICATION", True)

            try:
                user = get_user_model().objects.get(email=email)
                if user.is_verified:
                    content = {"detail": _("Email address already taken.")}
                    return Response(content, status=status.HTTP_400_BAD_REQUEST)

                # Delete old signup codes
                signup_code = SignupCode.objects.filter(user=user)
                signup_code.delete()

            except get_user_model().DoesNotExist:
                user = get_user_model().objects.create_user(email=email)

            # Set user fields provided
            if password:
                user.set_password(password)

            user.first_name = first_name
            user.last_name = last_name
            if not must_validate_email:
                user.is_verified = True

                ww_modal_params = {
                    "name": "userPreferencesOverlay", "options": {"tab": 0}}
                ww_modal_params_json = json.dumps(
                    ww_modal_params).replace(' ', '')
                workweek_url_next = \
                    urllib.parse.quote(
                        f"/dashboard/team-view?showModal={urllib.parse.quote(ww_modal_params_json)}")
                design_work_week_url = f"{settings.BASE_URL}/login?next={workweek_url_next}"

                invite_modal_params = {
                    "name": "addTeamMembersModal"}
                invite_modal_params_json = json.dumps(
                    invite_modal_params).replace(' ', '')
                invite_url_next = \
                    urllib.parse.quote(
                        f"/dashboard/team-view?showModal={urllib.parse.quote(invite_modal_params_json)}")
                invite_team_url = f"{settings.BASE_URL}/login?next={invite_url_next}"

                send_multi_format_email(
                    "welcome_email",
                    {
                        "email": user.email,
                        "first_name": user.first_name,
                        "last_name": user.last_name,
                        "user": {"id": str(user.id)},
                        "base_url": settings.BASE_URL,
                        "invite_team_url": invite_team_url,
                        "design_workweek_url": design_work_week_url
                    },
                    target_email=user.email,
                )
            user.save()

            if must_validate_email:
                client_ip, _routable = get_client_ip(request)
                AuthAuditLog.track(
                    user,
                    AuthAuditEventType.ACCOUNT_SIGNUP,
                    ip_address=client_ip,
                    ua_agent=request.META.get("HTTP_USER_AGENT"),
                )

                # Create and associate signup code
                signup_code = SignupCode.objects.create_signup_code(
                    user, client_ip)
                signup_code.send_signup_email()

            content = {
                "id": user.id,
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
            }
            return Response(content, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SignupVerify(APIView):
    permission_classes = (AllowAny,)

    def post(self, request, format=None):
        serializer = SignupVerificationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        code = serializer.data["code"]
        email = serializer.data["email"]
        user = get_user_model().objects.filter(email=email).first()
        if not user:
            return Response(
                {"detail": "Invalid email"}, status=status.HTTP_400_BAD_REQUEST
            )

        if user.is_verified:
            SignupCode.objects.filter(user=user).delete()
            return Response(
                {"detail": "User is already verified"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        signup_code = SignupCode.objects.filter(code=code).first()
        if not signup_code:
            return Response(
                {"detail": "Unable to verify account"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if signup_code.user.email != email:
            return Response(
                {"detail": "Email does not match verification code"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        verified, message = SignupCode.objects.set_user_is_verified(
            signup_code, request
        )

        if not verified:
            content = {"detail": _(message)}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)

        # check callback
        if auth_verify_callback_func is not None and hasattr(auth_verify_callback_func, '__call__'):
            auth_verify_callback_func(code)

        # Issue an auth token so that user can set password + other details
        token, _created = Token.objects.get_or_create(user=signup_code.user)
        django_login(request, signup_code.user,
                     backend="django.contrib.auth.backends.ModelBackend")

        signup_code.delete()

        content = {"success": _(message), "token": token.key}
        return Response(content, status=status.HTTP_200_OK)


class Login(APIView):
    permission_classes = (AllowAny,)
    serializer_class = LoginSerializer

    # Keep a pretty high tolerance for ip, since we have lots of corporate
    # users they might be behind a NAT and share a common IP.
    @method_decorator(ratelimit(key="ip", rate="10/m"))
    @method_decorator(ratelimit(key="post:email"))
    def post(self, request, format=None):
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            email = serializer.data["email"]
            email = email.lower()
            password = serializer.data["password"]
            user = authenticate(email=email, password=password)

            if user:
                if user.is_verified:
                    if user.is_active:
                        token, _created = Token.objects.get_or_create(
                            user=user)

                        client_ip, _routable = get_client_ip(request)
                        AuthAuditLog.track(
                            user,
                            AuthAuditEventType.LOGIN,
                            ip_address=client_ip,
                            ua_agent=request.META.get("HTTP_USER_AGENT"),
                        )
                        django_login(request, user)
                        return Response({"token": token.key}, status=status.HTTP_200_OK)
                    else:
                        content = {"detail": _("User account not active.")}
                        return Response(content, status=status.HTTP_401_UNAUTHORIZED)
                else:
                    content = {"detail": _("User account not verified.")}
                    return Response(content, status=status.HTTP_401_UNAUTHORIZED)
            else:
                # TODO: Log failed attempts and lock account/ask for 2fa
                #       after certain amount of failed ones.
                content = {"detail": _(
                    "Unable to login with provided credentials.")}
                return Response(content, status=status.HTTP_401_UNAUTHORIZED)

        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class Logout(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request, format=None):
        """
        Remove all auth tokens owned by request.user.
        """
        tokens = Token.objects.filter(user=request.user)
        for token in tokens:
            token.delete()
        content = {"success": _("User logged out.")}
        django_logout(request)
        return Response(content, status=status.HTTP_200_OK)


class PasswordReset(APIView):
    permission_classes = (AllowAny,)
    serializer_class = PasswordResetSerializer

    def post(self, request, format=None):
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            email = serializer.data["email"]

            try:
                user = get_user_model().objects.get(email=email)

                # Delete all unused password reset codes
                PasswordResetCode.objects.filter(user=user).delete()

                if user.is_verified and user.is_active:
                    password_reset_code = (
                        PasswordResetCode.objects.create_password_reset_code(
                            user)
                    )
                    password_reset_code.send_password_reset_email()
                    content = {"email": email}

                    client_ip, _routable = get_client_ip(request)
                    AuthAuditLog.track(
                        user,
                        AuthAuditEventType.RESET_PASSWORD_REQ,
                        ip_address=client_ip,
                        ua_agent=request.META.get("HTTP_USER_AGENT"),
                    )
                    return Response(content, status=status.HTTP_201_CREATED)

            except get_user_model().DoesNotExist:
                pass

            # Since this is AllowAny, don't give away error.
            content = {"detail": _("Password reset not allowed.")}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)

        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PasswordResetVerify(APIView):
    permission_classes = (AllowAny,)

    def get(self, request, format=None):
        code = request.GET.get("code", "")

        try:
            password_reset_code = PasswordResetCode.objects.get(code=code)

            # Delete password reset code if older than expiry period
            delta = date.today() - password_reset_code.created_at.date()
            if delta.days > PasswordResetCode.objects.get_expiry_period():
                password_reset_code.delete()
                raise PasswordResetCode.DoesNotExist()

            content = {"success": _("Email address verified.")}
            return Response(content, status=status.HTTP_200_OK)
        except PasswordResetCode.DoesNotExist:
            content = {"detail": _("Unable to verify user.")}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)


class PasswordResetVerified(APIView):
    permission_classes = (AllowAny,)
    serializer_class = PasswordResetVerifiedSerializer

    def post(self, request, format=None):
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            code = serializer.data["code"]
            password = serializer.data["password"]

            try:
                password_reset_code = PasswordResetCode.objects.get(code=code)
                password_reset_code.user.set_password(password)
                password_reset_code.user.save()

                # Delete password reset code just used
                password_reset_code.delete()

                content = {"success": _("Password reset.")}

                client_ip, _routable = get_client_ip(request)
                AuthAuditLog.track(
                    password_reset_code.user,
                    AuthAuditEventType.PASSWORD_UPDATED,
                    ip_address=client_ip,
                    ua_agent=request.META.get("HTTP_USER_AGENT"),
                )
                return Response(content, status=status.HTTP_200_OK)
            except PasswordResetCode.DoesNotExist:
                content = {"detail": _("Unable to verify user.")}
                return Response(content, status=status.HTTP_400_BAD_REQUEST)

        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class EmailChange(APIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = EmailChangeSerializer

    def post(self, request, format=None):
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            user = request.user

            current_password = serializer.data["current_password"]
            user = authenticate(email=user.email, password=current_password)
            if user is None:
                return Response(
                    {"details": "Invalid password login"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # Delete all unused email change codes
            EmailChangeCode.objects.filter(user=user).delete()

            email_new = serializer.data["email"]

            # TODO: This is a hack. The auth lib should not know about internal
            #       models in Alignment. But its the easiest way to achieve this
            #       check.
            valid_domains = set(
                d.lower()
                for d in user.company.domains.all().values_list("name", flat=True)
            )
            domain_portion = email_new.split("@")[-1].lower()
            if domain_portion not in valid_domains:
                return Response(
                    {"detail": "Not a valid email domain for you company"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                user_with_email = get_user_model().objects.get(email=email_new)
                if user_with_email.is_verified:
                    content = {"detail": _("Email address already taken.")}
                    return Response(content, status=status.HTTP_400_BAD_REQUEST)
                else:
                    # If the account with this email address is not verified,
                    # give this user a chance to verify and grab this email address
                    raise get_user_model().DoesNotExist

            except get_user_model().DoesNotExist:
                email_change_code = EmailChangeCode.objects.create_email_change_code(
                    user, email_new
                )

                email_change_code.send_email_change_emails()

                content = {"email": email_new}

                client_ip, _routable = get_client_ip(request)
                AuthAuditLog.track(
                    user,
                    AuthAuditEventType.CHANGE_EMAIL_REQ,
                    ip_address=client_ip,
                    ua_agent=request.META.get("HTTP_USER_AGENT"),
                )
                return Response(content, status=status.HTTP_201_CREATED)

        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class EmailChangeVerify(APIView):
    permission_classes = (AllowAny,)

    def get(self, request, format=None):
        code = request.GET.get("code", "")

        try:
            # Check if the code exists.
            email_change_code = EmailChangeCode.objects.get(code=code)

            # Check if the code has expired.
            delta = date.today() - email_change_code.created_at.date()
            if delta.days > EmailChangeCode.objects.get_expiry_period():
                email_change_code.delete()
                raise EmailChangeCode.DoesNotExist()

            # Check if the email address is being used by a verified user.
            try:
                user_with_email = get_user_model().objects.get(
                    email=email_change_code.email
                )
                if user_with_email.is_verified:
                    # Delete email change code since won't be used
                    email_change_code.delete()

                    content = {"detail": _("Email address already taken.")}
                    return Response(content, status=status.HTTP_400_BAD_REQUEST)
                else:
                    # If the account with this email address is not verified,
                    # delete the account (and signup code) because the email
                    # address will be used for the user who just verified.
                    user_with_email.delete()
            except get_user_model().DoesNotExist:
                pass

            # If all is well, change the email address.
            email_change_code.user.email = email_change_code.email
            email_change_code.user.save()

            # Delete email change code just used
            email_change_code.delete()

            content = {"success": _("Email address changed.")}

            client_ip, _routable = get_client_ip(request)
            AuthAuditLog.track(
                email_change_code.user,
                AuthAuditEventType.EMAIL_UPDATED,
                ip_address=client_ip,
                ua_agent=request.META.get("HTTP_USER_AGENT"),
            )
            return Response(content, status=status.HTTP_200_OK)
        except EmailChangeCode.DoesNotExist:
            content = {"detail": _("Unable to verify user.")}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)


class PasswordChange(APIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = PasswordChangeSerializer

    def post(self, request, format=None):
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            user = request.user

            if user.has_usable_password():
                current_password = serializer.data.get("current_password")
                if not current_password:
                    return Response(
                        {
                            "details": "Current password needs to be supplied for this user"
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                user = authenticate(
                    email=user.email, password=current_password)
                if user is None:
                    return Response(
                        {"details": "Invalid password login"},
                        status=status.HTTP_401_UNAUTHORIZED,
                    )

            password = serializer.data["password"]
            user.set_password(password)
            user.save()

            content = {"success": _("Password changed.")}

            client_ip, _routable = get_client_ip(request)
            AuthAuditLog.track(
                user,
                AuthAuditEventType.PASSWORD_UPDATED,
                ip_address=client_ip,
                ua_agent=request.META.get("HTTP_USER_AGENT"),
            )

            return Response(content, status=status.HTTP_200_OK)

        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserMe(APIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = UserSerializer

    def get(self, request, format=None):
        return Response(self.serializer_class(request.user).data)
